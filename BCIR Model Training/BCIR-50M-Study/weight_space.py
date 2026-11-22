"""Controlled weight programs and gradient-free edits; no claim of article replication."""
from common import *
import gc

def frozen_protocol():
    p=dict(schema='bcir.weight-program.study.v1',source_sha256=sha(Path(__file__)),
        common_sha256=sha(ROOT/'common.py'),training_protocol_sha256=sha(ROOT/'protocol.json'),
        scope='Agent-authored algorithms executed in Python with native C inference. No separate Astra calls. Small neural circuit compilation and planted-fault repair are distinct from natural capability improvement.',
        circuit='One fixed 3-8-2 ReLU full-adder, composed across eight input bit pairs. Zero-SGD programmed weights; same circuit compiler control, Gaussian-weight control, three same-architecture Adam/MSE baselines, 2000 full-truth-table updates. Exhaustive 65536 integer pairs. No model feedback before formula is frozen.',
        fault='For reference10m and selected50m, both seeds: one middle-layer MLP down-projection column multiplied by 256. Detector sees only each candidate down matrix, not clean weights or injected index: norm > 8*median(column norms) rescaled to median. No forwards or gradients to choose repair. Clean negative, sham, random-column and same-count same-norm random perturbation controls.',
        permutation='Middle FFN hidden coordinates jointly permuted in gate/up rows and down columns, and both Adam moments. Same weights in new coordinates, FP32 rounding may move. Down-only permutation is a damaging control. Compare logits/CE and one future update after inverse permutation.',
        natural='Deflate one middle MLP down projection along its leading left singular direction, found by 20 weight-only power iterations. Alpha [0,.1,.3,1], select minimum validation CE including no-op. Eight validation batches per candidate: 32 selection forwards. Matched-norm random direction is a control. Independent 20-step native Adam continuation on fresh train batches is an additional, unequal-compute reference. No gradients in edit arm.',
        validation=dict(split='validation',seed=771104,batches=8,tokens=2048),
        test=dict(split='test',seed=681104,batches=64,tokens=16384),
        outcomes='Held-out CE, token accuracy, KL(clean||edited), top-1 agreement, changed coordinates, validation/inference calls, edit time, and full raw metrics. Test outcomes never choose edits or repeat failed candidates.',
        evidence_limits='Two training seeds; byte language pilot heavily undertrained. Fault repair is deliberately favorable and not new knowledge. Arithmetic program has symbolic bit framing and circuit composition. No claim about factual editing, safety traits, reasoning, or published game/math benchmarks.')
    path=ROOT/'weight-protocol.json'
    if path.exists():
        if json.loads(path.read_text())!=p:raise ValueError('weight protocol changed')
    else:write_json(path,p)
    return p

def programmed_adder():
    patterns=((np.arange(8)[:,None]>>np.arange(3))&1).astype(np.float32)
    w1=(2*patterns-1).T.copy();b1=(.5-patterns.sum(1)).copy()
    totals=patterns.sum(1).astype(int)
    w2=(2*np.stack([totals%2,totals//2],1)).astype(np.float32);b2=np.zeros(2,np.float32)
    return [w1,b1,w2,b2]

def circuit_forward(x,p):return np.maximum(x@p[0]+p[1],0)@p[2]+p[3]

def exhaustive_add(p):
    a=np.repeat(np.arange(256),256);b=np.tile(np.arange(256),256)
    carry=np.zeros(65536,dtype=np.int64);out=np.zeros_like(carry)
    for bit in range(8):
        x=np.stack([(a>>bit)&1,(b>>bit)&1,carry],1).astype(np.float32)
        y=np.clip(np.rint(circuit_forward(x,p)),0,1).astype(np.int64)
        out+=y[:,0]<<bit;carry=y[:,1]
    out+=carry<<8
    return dict(correct=int((out==a+b).sum()),total=65536,accuracy=float((out==a+b).mean()))

def circuit_study():
    patterns=((np.arange(8)[:,None]>>np.arange(3))&1).astype(np.float32)
    target=np.stack([patterns.sum(1)%2,patterns.sum(1)//2],1)
    programmed=programmed_adder();results=dict(parameters=sum(p.size for p in programmed),
        programmed=exhaustive_add(programmed),compiled_control=exhaustive_add(programmed),trained=[])
    np.savez(ROOT/'checkpoints/programmed-full-adder.npz',**{f'p{i}':p for i,p in enumerate(programmed)})
    for seed in [411,412,413]:
        rng=np.random.default_rng(seed)
        p=[(rng.standard_normal((3,8))*.4).astype(np.float32),np.full(8,.1,np.float32),
           (rng.standard_normal((8,2))*.2).astype(np.float32),np.zeros(2,np.float32)]
        random_result=exhaustive_add(p);mom=[np.zeros_like(v) for v in p];var=[np.zeros_like(v) for v in p]
        t=time.monotonic();first=None
        for step in range(1,2001):
            pre=patterns@p[0]+p[1];hidden=np.maximum(pre,0);y=hidden@p[2]+p[3]
            dy=(y-target)/8
            dh=(dy@p[2].T)*(pre>0)
            grad=[patterns.T@dh,dh.sum(0),hidden.T@dy,dy.sum(0)]
            for i,g in enumerate(grad):
                mom[i]=.9*mom[i]+.1*g;var[i]=.999*var[i]+.001*g*g
                p[i]-=.01*(mom[i]/(1-.9**step))/(np.sqrt(var[i]/(1-.999**step))+1e-8)
            if first is None and np.array_equal(np.clip(np.rint(circuit_forward(patterns,p)),0,1),target):first=step
        results['trained'].append(dict(seed=seed,initial=random_result,exhaustive=exhaustive_add(p),
            first_full_adder_truth_table_correct=first,updates=2000,seconds=time.monotonic()-t,
            truth_table_mse=float(np.mean((circuit_forward(patterns,p)-target)**2))))
        np.savez(ROOT/'checkpoints'/f'trained-full-adder-{seed}.npz',**{f'p{i}':v for i,v in enumerate(p)})
    rng=np.random.default_rng(991)
    noise=[]
    for sigma in [.01,.1,.25]:
        x=np.repeat(patterns,1000,axis=0)+rng.normal(0,sigma,(8000,3))
        y=np.tile(target[:,None,:],(1,1000,1)).reshape(8000,2)
        accuracy=float(np.all(np.clip(np.rint(circuit_forward(x,programmed)),0,1)==y,axis=1).mean())
        noise.append(dict(sigma=sigma,accuracy=accuracy))
    results['off_binary_noise']=noise
    assert results['programmed']['correct']==65536
    write_json(ROOT/'results/circuit.json',results)
    print('circuit',json.dumps(results),flush=True)

def named(m,which='weights'):
    flat=views(m)[which]
    return {n:flat[o:o+c].reshape(s) for n,s,o,c in m.layout}

def detail(m,xy,reference=None):
    x,y=xy;losses=[];zs=[];kl=0.;agree=0;maxdiff=0.;correct=0;n=0
    for i,(xx,yy) in enumerate(zip(x,y)):
        raw=forward(m,xx);z=raw.astype(np.float64);mx=z.max(-1,keepdims=True)
        logp=z-mx-np.log(np.exp(z-mx).sum(-1,keepdims=True))
        losses.append(float(-np.take_along_axis(logp,yy[...,None],-1).mean()))
        correct+=int((z.argmax(-1)==yy).sum());n+=yy.size
        if reference is not None:
            r=reference[i].astype(np.float64);r-=r.max(-1,keepdims=True)
            logr=r-np.log(np.exp(r).sum(-1,keepdims=True))
            kl+=float((np.exp(logr)*(logr-logp)).sum())
            agree+=int((r.argmax(-1)==z.argmax(-1)).sum())
            maxdiff=max(maxdiff,float(np.max(np.abs(raw-reference[i]))))
        else:zs.append(raw)
    result=dict(ce=float(np.mean(losses)),accuracy=correct/n,tokens=n,batch_ce=losses)
    if reference is not None:result.update(kl_from_clean=kl/n,top1_agreement=agree/n,max_abs_logit_delta=maxdiff)
    return result,zs

def repair_down(matrix):
    """Weight-only input. Neither original matrix nor fault location is an argument."""
    norms=np.linalg.norm(matrix.astype(np.float64),axis=0);median=float(np.median(norms))
    flagged=np.flatnonzero(norms>8*median)
    for j in flagged:matrix[:,j]*=np.float32(median/norms[j])
    return dict(flagged=flagged.tolist(),median_norm=median,pre_norms=norms[flagged].tolist())

def edit_case(name,seed,p):
    result_path=ROOT/'results'/f'weights-{name}-{seed}.json'
    if result_path.exists():return
    d=corpus();test=batches(d['test'],p['test']['seed'],p['test']['batches'])
    val=batches(d['validation'],p['validation']['seed'],p['validation']['batches'])
    m=model(name,seed);cp=ROOT/'checkpoints'/f'{name}-{seed}-best.npz';meta=restore(m,cp)
    weights=views(m)['weights'];clean=weights.copy();layer=m.spec.n_layers//2
    prefix=f'model.layers.{layer}.';down_name=prefix+'mlp.down_proj.weight'
    down=named(m)[down_name];original=down.copy();rng=np.random.default_rng(seed+333)
    result=dict(name=name,seed=seed,parameters=count(name),checkpoint_sha256=sha(cp),checkpoint_step=meta['step'],
        repairs={},natural={},permutation={},test_seed=p['test']['seed'],protocol_sha256=sha(ROOT/'weight-protocol.json'))
    clean_result,reference=detail(m,test);result['clean']=clean_result
    result['repairs']['sham']=detail(m,test,reference)[0]
    clean_detection=repair_down(down);result['repairs']['clean_negative_detector']=clean_detection
    result['repairs']['clean_negative']=detail(m,test,reference)[0]
    weights[:]=clean
    column=int(rng.integers(down.shape[1]));down[:,column]*=256
    faulty=down.copy();result['repairs']['fault']=detail(m,test,reference)[0]
    t=time.monotonic();diagnosis=repair_down(down);edit_seconds=time.monotonic()-t;repaired=down.copy()
    delta=repaired-faulty;changed=int(np.count_nonzero(delta));edit_norm=float(np.linalg.norm(delta.astype(np.float64)))
    result['repairs']['diagnosis']=dict(**diagnosis,seconds=edit_seconds,injected_column=column,
        changed_parameters=changed,changed_percent=100*changed/count(name),edit_l2=edit_norm,
        repair_used_clean_checkpoint=False,repair_probe_calls=0)
    result['repairs']['repair']=detail(m,test,reference)[0]
    np.savez(ROOT/'checkpoints'/f'repair-patch-{name}-{seed}.npz',tensor=down_name,column=column,
        before=faulty[:,column],after=repaired[:,column],clean_checkpoint_sha256=sha(cp))
    down[:]=faulty;other=(column+37)%down.shape[1];down[:,other]=0
    result['repairs']['random_column_zero']=detail(m,test,reference)[0]
    down[:]=faulty;random_delta=rng.standard_normal(down.shape[0]);random_delta*=edit_norm/np.linalg.norm(random_delta)
    down[:,other]+=random_delta.astype(np.float32)
    result['repairs']['same_count_norm_random']=detail(m,test,reference)[0]
    weights[:]=clean
    perm=rng.permutation(down.shape[1]);inverse=np.argsort(perm)
    # Save no alternate full 50M copies: transformations act only on one FFN and moments.
    def permute(order):
        for kind in ['weights','moment1','moment2']:
            tensors=named(m,kind)
            for suffix in ['mlp.gate_proj.weight','mlp.up_proj.weight']:
                a=tensors[prefix+suffix];a[:]=a[order]
            a=tensors[down_name];a[:]=a[:,order]
    permute(perm)
    result['permutation']['joint']=detail(m,test,reference)[0]
    permute(inverse)
    down[:]=original[:,perm]
    result['permutation']['down_only']=detail(m,test,reference)[0]
    restore(m,cp)
    # A future update under permuted optimizer coordinates, then inverse mapping.
    update_data=batches(d['train'],seed+9090,1)
    m.run(update_data[0].ravel().tolist(),update_data[1].ravel().tolist(),steps=1,optimizer=NativeAdamW(lr=6e-5))
    updated={k:views(m)[k].copy() for k in ['weights','moment1','moment2']}
    restore(m,cp);permute(perm)
    m.run(update_data[0].ravel().tolist(),update_data[1].ravel().tolist(),steps=1,optimizer=NativeAdamW(lr=6e-5))
    permute(inverse)
    result['permutation']['future_update_max_abs']={k:float(np.max(np.abs(views(m)[k]-a))) for k,a in updated.items()}
    del updated;restore(m,cp)
    # Natural clean-model proposal: no injected anomaly and no known correct target weights.
    down=named(m)[down_name];original=down.copy();t=time.monotonic()
    direction=rng.standard_normal(down.shape[0]);direction/=np.linalg.norm(direction)
    for _ in range(20):
        direction=original@(original.T@direction);direction/=np.linalg.norm(direction)
    rank_one=np.outer(direction,direction@original).astype(np.float32)
    proposals=[]
    for alpha in [0.,.1,.3,1.]:
        down[:]=original-np.float32(alpha)*rank_one
        proposals.append(dict(alpha=alpha,validation=evaluate(m,val)))
    selected=min(proposals,key=lambda a:a['validation']['ce']);alpha=selected['alpha']
    down[:]=original-np.float32(alpha)*rank_one
    result['natural'].update(proposals=proposals,selected_alpha=alpha,validation_selection_forward_batches=32,
        gradients_used=0,selection_seconds=time.monotonic()-t,changed_parameters=int(np.count_nonzero(down-original)),
        selected=detail(m,test,reference)[0])
    np.savez(ROOT/'checkpoints'/f'natural-patch-{name}-{seed}.npz',tensor=down_name,alpha=alpha,
        left=direction,right=direction@original,clean_checkpoint_sha256=sha(cp))
    rand=rng.standard_normal(down.shape[0]);rand/=np.linalg.norm(rand)
    random_rank=np.outer(rand,rand@original)
    target_norm=float(np.linalg.norm(alpha*rank_one.astype(np.float64)))
    random_rank*=target_norm/(np.linalg.norm(random_rank) or 1)
    down[:]=original-random_rank.astype(np.float32)
    result['natural']['matched_norm_random']=detail(m,test,reference)[0]
    restore(m,cp);xy=batches(d['train'],seed,620)
    t=time.monotonic()
    m.run(xy[0][600:620].ravel().tolist(),xy[1][600:620].ravel().tolist(),steps=20,optimizer=NativeAdamW(lr=6e-5))
    result['natural']['adam20_seconds']=time.monotonic()-t
    result['natural']['adam20']=detail(m,test,reference)[0]
    result['rss_max_mib']=rss_mib()
    write_json(result_path,result)
    print('weights',json.dumps(dict(name=name,seed=seed,clean_ce=result['clean']['ce'],fault_ce=result['repairs']['fault']['ce'],
        repair_ce=result['repairs']['repair']['ce'],natural_alpha=alpha,natural_ce=result['natural']['selected']['ce'],
        adam20_ce=result['natural']['adam20']['ce'])),flush=True)
    m.close();del m,weights,clean,reference;gc.collect()

def all_baselines(p):
    test=batches(corpus()['test'],p['test']['seed'],p['test']['batches']);results=[]
    for path in sorted((ROOT/'results').glob('train-*.json')):
        r=json.loads(path.read_text())
        m=model(r['name'],r['seed']);restore(m,ROOT/r['best_checkpoint']['path'])
        metric=evaluate(m,test);m.close();del m;gc.collect()
        results.append(dict(name=r['name'],seed=r['seed'],parameters=r['parameters'],steps=r['completed_steps'],
            best_step=r['best_step'],validation_ce=r['best_ce'],test=metric,tokens=r['tokens'],
            train_seconds=r['native_train_seconds'],memory_bytes=r['memory_bytes'],rss_max_mib=r['rss_max_mib']))
        write_json(ROOT/'results/test-baselines.json',results)
        print('baseline',r['name'],r['seed'],metric['ce'],flush=True)

if __name__=='__main__':
    p=frozen_protocol()
    if not (ROOT/'results/selection.json').exists():raise ValueError('finish training and selection first')
    all_baselines(p)
    circuit_study()
    chosen=json.loads((ROOT/'results/selection.json').read_text())['chosen']
    for name in ['reference10m',chosen]:
        for seed in SEEDS:edit_case(name,seed,p)
