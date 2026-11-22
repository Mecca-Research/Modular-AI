"""Bounded PR822 dispatch measurement and exact native/program parity at the new scale."""
from common import *
from bcir.hosted.models.native_choose import measure,choose_dispatch,trainer_shape
from bcir.hosted.models.native_launch import step_program,program_words
from array import array
import gc

def state_hashes(m):
    return {k:hashlib.sha256(memoryview(views(m)[k]).cast('B')).hexdigest()
            for k in ['weights','gradients','moment1','moment2']}

def main():
    p=dict(schema='bcir.scaling.execution-check.v1',source_sha256=sha(Path(__file__)),rounds=8,
        kinds=['dispatch:down.fwd'],purpose='One bounded dispatch site family, not exhaustive tuning; quality checkpoints untouched.',
        parity='Fixed C step versus selected launch program from identical restored full state, one update, all four persistent-array hashes and event equality.',
        scaling_checks='Selected 50M: one maximum-magnitude coordinate per embedding/middle-layer/final-norm span checked by finite differences; causal-prefix equality; forced 850MiB auto-recompute step compared bitwise with ample-buffer step.',
        timing='Choice table intervals from native job clock. No end-to-end speedup inferred from a single changed family.')
    write_json(ROOT/'execution-protocol.json',p)
    timed=NativeDecoder.build(ROOT/'build',cc='gcc',probe='time')
    chosen=json.loads((ROOT/'results/selection.json').read_text())['chosen']
    data=corpus()
    for name in ['reference10m',chosen]:
        m=NativeDecoder(timed,spec(name),batch_size=4,context_length=64,threads=2,
            seed=SEEDS[0],contract=CONTRACT,max_memory_bytes=4*1024**3)
        cp=ROOT/'checkpoints'/f'{name}-{SEEDS[0]}-best.npz';restore(m,cp)
        raw=[];table=measure(m,rounds=8,kinds=p['kinds'],raw=raw)
        shape=trainer_shape(m,instances=True);picks=choose_dispatch(table,m.spec,shape)
        words=array('Q',program_words(step_program(m.spec,4,64,**m.program_decisions(),dispatch=picks)))
        (ROOT/'results'/f'dispatch-{name}.json').write_text(table.to_json()+'\n')
        write_json(ROOT/'results'/f'dispatch-raw-{name}.json',raw)
        xy=batches(data['train'],991104,1)
        x,y=xy[0].ravel().tolist(),xy[1].ravel().tolist()
        restore(m,cp);fixed=m.run(x,y,steps=1,optimizer=NativeAdamW(lr=6e-5));expected=state_hashes(m)
        restore(m,cp);program=m.run_program(words,x,y,steps=1,optimizer=NativeAdamW(lr=6e-5));actual=state_hashes(m)
        identity=expected==actual and fixed==program
        assert identity,(name,picks)
        record=dict(name=name,picks=picks,
            native_program_state_exact=identity,expected=expected,actual=actual,event=fixed,
            instance_families=m.instance_families(),program_words=len(words),shape=shape,
            clock_library_sha256=sha(timed),checkpoint_sha256=sha(cp),measurement_rounds=8)
        if name==chosen:
            restore(m,cp)
            z=forward(m,xy[0][0]);altered=xy[0][0].copy();altered[:,32:]=(altered[:,32:]+71)%256
            record['scale_causal_prefix_exact']=bool(np.array_equal(z[:,:32],forward(m,altered)[:,:32]))
            assert record['scale_causal_prefix_exact']
            m.backward(x,y);weights=views(m)['weights'];grad=views(m)['gradients']
            probes=[]
            for tensor,shape0,offset,n in m.layout:
                if not (tensor in ['model.embed_tokens.weight','model.norm.weight'] or tensor.startswith(f'model.layers.{m.spec.n_layers//2}.')):continue
                i=offset+int(np.argmax(np.abs(grad[offset:offset+n])));analytic=float(grad[i]);old=float(weights[i]);h=.002
                weights[i]=old+h;hi_weight=float(weights[i]);hi=evaluate(m,xy)['ce']
                weights[i]=old-h;lo_weight=float(weights[i]);lo=evaluate(m,xy)['ce'];weights[i]=old
                numerical=(hi-lo)/(hi_weight-lo_weight);error=abs(numerical-analytic)
                probes.append(dict(tensor=tensor,index=i,analytic=analytic,numerical=numerical,absolute_error=error))
                assert error<=max(5e-5,.03*abs(analytic)),(tensor,analytic,numerical)
            record['scale_finite_difference_probes']=probes
            m.close();del m;gc.collect()
            m=NativeDecoder(timed,spec(name),batch_size=4,context_length=64,threads=2,seed=SEEDS[0],
                contract=CONTRACT,max_memory_bytes=850*1024**2,recompute='auto')
            restore(m,cp);compact=m.run(x,y,steps=1,optimizer=NativeAdamW(lr=6e-5));compact_hashes=state_hashes(m)
            record['recompute']=dict(budget_mib=850,memory_bytes=m.memory_bytes,layer_mask=m.recompute,
                state_exact=compact_hashes==expected and compact==fixed,hashes=compact_hashes)
            assert record['recompute']['state_exact'] and m.recompute!=0
        write_json(ROOT/'results'/f'execution-{name}.json',record)
        print('execution',name,'PASS',json.dumps(picks),flush=True)
        m.close();del m;gc.collect()

if __name__=='__main__':main()
