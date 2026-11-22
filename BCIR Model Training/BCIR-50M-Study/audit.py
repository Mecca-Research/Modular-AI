"""Final consistency/provenance audit, separate from hypothesis outcomes."""
from common import *
import subprocess,gc

def write_source_manifest():
    rows={}
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file():continue
        rel=path.relative_to(ROOT)
        if rel.parts[0] in ['build','deps','data','__pycache__']:continue
        if path.name in ['SOURCE_MANIFEST.json','protocol-preview.json']:continue
        if rel.parts[0]=='checkpoints' and path.stat().st_size>1024**2:continue
        rows[str(rel)]=dict(bytes=path.stat().st_size,sha256=sha(path))
    write_json(ROOT/'SOURCE_MANIFEST.json',dict(schema='bcir.study.source-manifest.v1',files=rows,
        note='Final artifacts, not a claim that later analysis/report code existed before training. Frozen model/experiment hashes live in the protocols. Large state hashes live in training records and artifacts.json.'))

def main():
    expected='80e9507531b7597ef81d0348db75f0fd5dcd8134'
    assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip()==expected
    assert not subprocess.check_output(['git','status','--porcelain'],cwd=REPO,text=True).strip()
    subprocess.run(['git','diff','--check'],cwd=REPO,check=True)
    protocol=json.loads((ROOT/'protocol.json').read_text())
    for file,digest in protocol['source_hashes'].items():assert sha(ROOT/file)==digest,file
    build=json.loads((ROOT/'results/build.json').read_text())
    for file,digest in build['sources'].items():assert sha(REPO/'runtime/c'/file)==digest,file
    assert sha(LIB)==build['library_sha256']
    for file,script in [('weight-protocol.json','weight_space.py'),('extension-protocol.json','extend_architectures.py'),('execution-protocol.json','execution_check.py')]:
        assert json.loads((ROOT/file).read_text())['source_sha256']==sha(ROOT/script),script
    records={};batch_hashes={s:set() for s in SEEDS};checkpoints=[]
    for path in sorted((ROOT/'results').glob('train-*.json')):
        r=json.loads(path.read_text());key=(r['name'],r['seed']);records[key]=r
        assert r['completed_steps']==600 and r['tokens']==153600,key
        assert len(r['events'])==600 and [e['step'] for e in r['events']]==list(range(1,601)),key
        assert all(np.isfinite(e['loss']) and np.isfinite(e['grad_norm']) for e in r['events']),key
        assert r['parameters']==count(r['name']),key
        batch_hashes[r['seed']].add(r['batch_sha256'])
        for which in ['best_checkpoint','last_checkpoint']:
            record=r[which];cp=ROOT/record['path'];assert sha(cp)==record['sha256'],str(cp)
            with np.load(cp,allow_pickle=False) as f:
                meta=json.loads(str(f['metadata']))
                assert meta['step']==(r['best_step'] if which=='best_checkpoint' else 600),key
                assert meta['decoder']==asdict(spec(r['name'])) and meta['contract']==CONTRACT,key
                for name in ['weights','moment1','moment2']:
                    a=f[name]
                    assert a.shape==(r['parameters'],) and a.dtype==np.float32 and np.isfinite(a).all(),key
                    if name=='moment2':assert (a>=0).all(),key
                    del a
            checkpoints.append(record['path'])
    assert len(records)==10 and all(len(v)==1 for v in batch_hashes.values())
    selection=json.loads((ROOT/'results/selection.json').read_text());chosen=selection['chosen']
    scores={n:float(np.mean([records[(n,s)]['best_ce'] for s in SEEDS])) for n in ['wide50m','deep50m']}
    assert chosen==min(scores,key=scores.get)
    tests=json.loads((ROOT/'results/test-baselines.json').read_text());test_map={(r['name'],r['seed']):r for r in tests}
    assert set(test_map)==set(records)
    assert all(r['test']['tokens']==16384 and len(r['test']['batch_ce'])==64 for r in tests)
    editfiles=list((ROOT/'results').glob('weights-*.json'));assert len(editfiles)==4
    for path in editfiles:
        r=json.loads(path.read_text());key=(r['name'],r['seed']);base=test_map[key]
        assert abs(r['clean']['ce']-base['test']['ce'])<1e-9,key
        assert r['checkpoint_sha256']==records[key]['best_checkpoint']['sha256'],key
        assert r['repairs']['sham']['max_abs_logit_delta']==0 and r['repairs']['sham']['top1_agreement']==1,key
        assert r['natural']['gradients_used']==0 and r['natural']['validation_selection_forward_batches']==32,key
        assert r['permutation']['joint']['max_abs_logit_delta']<3e-4,key
    for n in ['reference10m',chosen]:
        r=json.loads((ROOT/'results'/f'execution-{n}.json').read_text())
        assert r['native_program_state_exact'] and r['expected']==r['actual'],n
        if n==chosen:assert r['recompute']['state_exact'] and r['scale_causal_prefix_exact']
    write_json(ROOT/'results/final-audit.json',dict(status='PASS',source_commit=expected,worktree_clean=True,
        runs=len(records),quality_updates=6000,supervised_targets=1536000,checkpoint_files_checked=len(checkpoints),
        full_state_arrays_finite=True,moment2_nonnegative=True,paired_batch_hashes=True,
        source_protocol_hashes_match=True,selected_architecture=chosen,selection_validation_means=scores,
        test_groups=len(tests),weight_edit_groups=len(editfiles),sham_exact=True,program_state_exact=True))
    write_source_manifest()
    print('PASS provenance, 10 runs / 6000 updates, 20 full-state files, pairing, test identities, edit controls and program parity')

if __name__=='__main__':main()
