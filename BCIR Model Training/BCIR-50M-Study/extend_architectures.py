"""Pre-test extension: resolve the ambiguous initial 50M architecture screen."""
from common import *
from train import train

def freeze_extension():
    p=dict(schema='bcir.scaling.extension.v1',source_sha256=sha(Path(__file__)),
        parent_protocol_sha256=sha(ROOT/'protocol.json'),
        reason='The first 200-update screen differed by only 0.01159 nats and 1.4% mean training time; insufficient basis for a strong architecture choice.',
        declared_before='Any final-test evaluation and either wide50m 600-step extension/new-seed result.',
        design='Complete both 50M recipes to 600 updates on both seeds, keeping original batches, optimizer, schedule, checkpoint rule and numerical family.',
        selection='Choose lower two-seed mean of validation-selected checkpoint CE. Report time/memory and per-seed directions separately. No claim of globally optimal architecture or statistically resolved winner from two seeds.',
        original_tie_break_note='Original code used mean training time for the tie, although prose said median step time. The independent five-update median also ranked deep50m faster, so original candidate identity was unaffected. This extension removes that timing tie-break from the final recommendation.')
    path=ROOT/'extension-protocol.json'
    if path.exists():
        if json.loads(path.read_text())!=p:raise ValueError('extension changed')
    else:write_json(path,p)
    return p

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--freeze-only',action='store_true');a=p.parse_args()
    freeze_extension()
    if not a.freeze_only:
        original=ROOT/'results/initial-selection.json'
        if not original.exists():write_json(original,json.loads((ROOT/'results/selection.json').read_text()))
        for name in ['wide50m','deep50m']:
            for seed in SEEDS:train(name,seed,600)
        rows=[]
        for name in ['wide50m','deep50m']:
            runs=[json.loads((ROOT/'results'/f'train-{name}-{seed}.json').read_text()) for seed in SEEDS]
            rows.append(dict(name=name,validation_ce=[r['best_ce'] for r in runs],
                mean_validation_ce=float(np.mean([r['best_ce'] for r in runs])),
                train_seconds=[r['native_train_seconds'] for r in runs]))
        chosen=min(rows,key=lambda r:r['mean_validation_ce'])['name']
        write_json(ROOT/'results/selection.json',dict(chosen=chosen,source='pre-test 600-step two-seed extension',candidates=rows,
            protocol_sha256=sha(ROOT/'extension-protocol.json')))
        print('FINAL ARCHITECTURE',chosen,json.dumps(rows),flush=True)
