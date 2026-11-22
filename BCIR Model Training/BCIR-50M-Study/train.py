from common import *
import argparse,gc

def freeze():
    source={p.name:sha(p) for p in [ROOT/'common.py',ROOT/'train.py',ROOT/'validate.py']}
    p=dict(schema='bcir.scaling.study.v1',source_commit='80e9507531b7597ef81d0348db75f0fd5dcd8134',
      source_hashes=source,architectures={n:dict(**asdict(spec(n)),parameters=count(n)) for n in ARCH},
      protocol='Exploratory CPU early-learning pilot; no claim of converged or compute-optimal pretraining.',
      seeds=SEEDS,threads=2,batch_size=4,context_length=64,steps=600,tokens_per_run=153600,
      optimizer=dict(lr=.0006,min_lr=.00006,warmup=30,beta1=.9,beta2=.95,epsilon=1e-8,weight_decay=.1,grad_clip=1.0),
      contract=CONTRACT,validation_batches=16,validation_seed=671104,test_batches=64,test_seed=681104,
      checkpoint_rule='minimum validation CE at steps 100,200,...600; initial evaluation is diagnostic',
      architecture_selection='Two 50M candidates run 200 updates on seed 20261104. Prefer lower validation CE; if difference <=.02 nats prefer faster median step. Then finish selected candidate to 600 and independently train second seed. All 10M candidates and sub-million pilot run 600 steps on both seeds.',
      comparison='Matched token/update counts for completed runs; candidate screening is exploratory. Test outcomes cannot select recipes, checkpoints, edits, or learning rates.',
      data=json.loads((ROOT/'results/data.json').read_text()),
      execution='Current unmodified native C forward/backward, GQA/RoPE/RMSNorm/SwiGLU/tied byte embeddings and native AdamW. No PyTorch training.')
    target=ROOT/'protocol.json'
    if target.exists():
        if json.loads(target.read_text())!=p:raise ValueError('frozen protocol differs')
    else:write_json(target,p)
    return p

def train(name,seed,stop=600):
    protocol=freeze();out=ROOT/'results'/f'train-{name}-{seed}.json'
    if out.exists() and json.loads(out.read_text())['completed_steps']>=stop:return json.loads(out.read_text())
    d=corpus();xy=batches(d['train'],seed,600);val=batches(d['validation'],protocol['validation_seed'],16)
    m=model(name,seed);v=views(m);path=ROOT/'checkpoints'/f'{name}-{seed}-last.npz'
    bestpath=ROOT/'checkpoints'/f'{name}-{seed}-best.npz'
    if out.exists():
        r=json.loads(out.read_text());restore(m,path);start=r['completed_steps']
    else:
        start=0;r=dict(name=name,seed=seed,parameters=count(name),spec=asdict(m.spec),memory_bytes=m.memory_bytes,
           contract=m.contract,initial_validation=evaluate(m,val),best_ce=1e30,events=[],validation=[],
           batch_sha256=hashlib.sha256(xy[0].tobytes()+xy[1].tobytes()).hexdigest(),native_train_seconds=0.,completed_steps=0)
    opt=NativeAdamW(lr=.0006,beta1=.9,beta2=.95,epsilon=1e-8,weight_decay=.1,grad_clip=1.)
    for first in range(start,stop,20):
        last=min(first+20,stop)
        rates=[.0006*(i+1)/30 if i<30 else .00006+.5*(.0006-.00006)*(1+np.cos(np.pi*(i-30)/(600-30))) for i in range(first,last)]
        t=time.monotonic()
        ev=m.run(xy[0][first:last].ravel().tolist(),xy[1][first:last].ravel().tolist(),steps=last-first,optimizer=opt,learning_rates=rates)
        seconds=time.monotonic()-t;r['native_train_seconds']+=seconds
        r['events']+=list(ev);r['completed_steps']=last;r['tokens']=last*256;r['rss_max_mib']=rss_mib()
        if last%100==0 or last==stop:
            metric=evaluate(m,val);metric.update(step=last,elapsed_train_seconds=r['native_train_seconds'])
            r['validation'].append(metric)
            if metric['ce']<r['best_ce']:
                r['best_ce']=metric['ce'];r['best_step']=last;r['best_checkpoint']=checkpoint(m,bestpath)
            r['last_checkpoint']=checkpoint(m,path)
            write_json(out,r)
            print(json.dumps(dict(name=name,seed=seed,step=last,training_loss=ev[-1]['loss'],validation_ce=metric['ce'],
                tokens=r['tokens'],train_seconds=r['native_train_seconds'],rss_mib=rss_mib())),flush=True)
    m.close();del m;gc.collect();return r

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--freeze',action='store_true');p.add_argument('--name',choices=list(ARCH));p.add_argument('--seed',type=int,default=SEEDS[0]);p.add_argument('--stop',type=int,default=600);p.add_argument('--ladder',action='store_true');a=p.parse_args()
    if a.freeze:print(json.dumps(freeze(),indent=2))
    elif a.ladder:
        freeze()
        for name in ['pilot1m','reference10m','wide10m']:
            for seed in SEEDS:train(name,seed)
        trials=[train(n,SEEDS[0],200) for n in ['wide50m','deep50m']]
        c1,c2=trials
        if abs(c1['best_ce']-c2['best_ce'])>.02:chosen=min(trials,key=lambda r:r['best_ce'])['name']
        else:chosen=min(trials,key=lambda r:r['native_train_seconds']/200)['name']
        write_json(ROOT/'results/selection.json',dict(chosen=chosen,source='validation-only seed0 200-step screen',
            candidates=[dict(name=r['name'],ce=r['best_ce'],train_seconds=r['native_train_seconds']) for r in trials]))
        for seed in SEEDS:train(chosen,seed,600)
    else:train(a.name,a.seed,a.stop)
