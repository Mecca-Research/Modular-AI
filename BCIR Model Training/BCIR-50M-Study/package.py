"""Archive user deliverables after all heavy training/verification has ended."""
from common import *
import zipfile,tempfile

DEST=ROOT.parent/'bcir-deliverables-2026-11-04'
DEST.mkdir(exist_ok=True)

def zip_array(z,name,a):
    with z.open(name,'w',force_zip64=True) as f:np.save(f,a,allow_pickle=False)

def main():
    artifact=[]
    small=[]
    for path in sorted((ROOT/'results').glob('train-*.json')):
        r=json.loads(path.read_text());cp=ROOT/r['best_checkpoint']['path']
        if r['parameters']<40_000_000:small.append(cp);continue
        identity=f"BCIR_{r['name']}_{r['seed']}_step{r['best_step']}"
        with np.load(cp,allow_pickle=False) as f:
            meta=json.loads(str(f['metadata']));records={}
            for key in ['weights','moment1','moment2']:
                a=f[key]
                records[key]=dict(file=key+'.npy',shape=list(a.shape),dtype=str(a.dtype),
                    sha256=hashlib.sha256(memoryview(a).cast('B')).hexdigest())
            manifest=dict(schema='bcir.study.checkpoint-parts.v1',metadata=meta,arrays=records,
                original_checkpoint_sha256=sha(cp),training_protocol_sha256=sha(ROOT/'protocol.json'))
            for label,keys in [('weights',['weights']),('optimizer',['moment1','moment2'])]:
                output=DEST/f'{identity}_{label}.zip'
                with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
                    z.writestr('checkpoint-parts.json',json.dumps(manifest,indent=2)+'\n')
                    z.write(ROOT/'restore_checkpoint.py','restore_checkpoint.py')
                    for key in keys:zip_array(z,records[key]['file'],f[key])
                artifact.append(dict(filename=output.name,bytes=output.stat().st_size,sha256=sha(output),
                    model=r['name'],seed=r['seed'],step=r['best_step'],part=label))
                print('archived',output.name,output.stat().st_size,flush=True)
    output=DEST/'BCIR_Smaller_Model_States_2026-11-04.zip'
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
        for path in small:z.write(path,path.name)
    artifact.append(dict(filename=output.name,bytes=output.stat().st_size,sha256=sha(output),part='smaller full states'))
    write_json(ROOT/'artifacts.json',artifact)
    # Checks every stored byte's ZIP checksum and the manifest before delivery.
    for row in artifact:
        with zipfile.ZipFile(DEST/row['filename']) as z:
            bad=z.testzip()
            if bad:raise ValueError('corrupt archive '+bad)
    # Exercise the published reassembly command on one actual selected 50M full state.
    from restore_checkpoint import restore_parts
    chosen=json.loads((ROOT/'results/selection.json').read_text())['chosen']
    row=json.loads((ROOT/'results'/f'train-{chosen}-{SEEDS[0]}.json').read_text())
    identity=f"BCIR_{chosen}_{SEEDS[0]}_step{row['best_step']}"
    with tempfile.TemporaryDirectory(prefix='bcir-checkpoint-roundtrip-',dir=ROOT.parent) as temp:
        parts=Path(temp)/'parts';parts.mkdir()
        for label in ['weights','optimizer']:
            with zipfile.ZipFile(DEST/f'{identity}_{label}.zip') as z:z.extractall(parts)
        restored=Path(temp)/'restored.npz';restore_parts(parts,restored)
        with np.load(restored,allow_pickle=False) as actual,np.load(ROOT/row['best_checkpoint']['path'],allow_pickle=False) as expected:
            assert json.loads(str(actual['metadata']))==json.loads(str(expected['metadata']))
            for key in ['weights','moment1','moment2']:assert np.array_equal(actual[key],expected[key]),key
    write_json(ROOT/'results/checkpoint-delivery-audit.json',dict(status='PASS',archives_crc_checked=len(artifact),
        roundtrip_model=chosen,seed=SEEDS[0],step=row['best_step'],metadata_exact=True,
        weights_exact=True,moment1_exact=True,moment2_exact=True,
        note='The public restore_checkpoint command verified array hashes and rebuilt an exact selected 50M training state. All state archives passed ZIP checksum reads.'))
    from audit import write_source_manifest
    write_source_manifest()
    # Include code, protocols, logs, summaries, plots and small analytical patches.
    workbench=DEST/'BCIR_50M_Study_2026-11-04.zip'
    with zipfile.ZipFile(workbench,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as z:
        for path in sorted(ROOT.rglob('*')):
            if not path.is_file():continue
            rel=path.relative_to(ROOT)
            if rel.parts[0] in ['build','deps','data','__pycache__']:continue
            if rel.parts[0]=='checkpoints' and path.stat().st_size>1024**2:continue
            if path.name=='protocol-preview.json':continue
            z.write(path,str(rel))
    with zipfile.ZipFile(workbench) as z:assert z.testzip() is None
    print('workbench',workbench.stat().st_size,flush=True)

if __name__=='__main__':main()
