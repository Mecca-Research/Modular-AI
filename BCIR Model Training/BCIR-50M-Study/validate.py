from common import *
from bcir.frontends.models.decode import decoder_forward_reference

s=DecoderSpec(vocab_size=16,d_model=16,n_heads=2,n_kv_heads=1,n_layers=1,d_ff=24,activation='silu_gate')
def make(threads=1):return NativeDecoder(LIB,s,batch_size=1,context_length=5,seed=118,threads=threads,contract=CONTRACT)
m=make();x=np.array([[1,4,7,2,9]],np.uint32);y=np.array([[4,7,2,9,3]],np.uint32)
z=forward(m,x)
hidden=np.array(decoder_forward_reference(x.ravel().tolist(),s,m.decoder_weights())).reshape(1,5,s.d_model)
embedding=views(m)['weights'][:s.vocab_size*s.d_model].reshape(s.vocab_size,s.d_model).astype(np.float64)
ref=hidden@embedding.T
err=float(np.max(np.abs(z-ref)));assert err<2e-6,err
changed=x.copy();changed[0,3:]=[11,12]
causal=bool(np.array_equal(z[:,:3],forward(m,changed)[:,:3]));assert causal
loss=m.backward(x.ravel().tolist(),y.ravel().tolist());g=views(m)['gradients'].copy();w=views(m)['weights']
probes=[]
def ce():
    a=forward(m,x).astype(np.float64);a-=a.max(-1,keepdims=True)
    return float((np.log(np.exp(a).sum(-1))-np.take_along_axis(a,y[...,None],-1)[...,0]).mean())
for name,shape,off,n in m.layout:
    i=off+n//2;old=float(w[i]);h=.002
    w[i]=old+h;hi=ce();w[i]=old-h;lo=ce();w[i]=old
    numerical=(hi-lo)/(2*h);delta=abs(float(g[i])-numerical)
    probes.append(dict(name=name,index=i,analytic=float(g[i]),numerical=numerical,absolute_error=delta))
    assert delta<3e-4,(name,delta)
m.close()
a=make(1);b=make(2);xx=np.tile(x,(3,1,1)).ravel().tolist();yy=np.tile(y,(3,1,1)).ravel().tolist()
a.run(xx,yy,steps=3);b.run(xx,yy,steps=3)
thread_identity=all(np.array_equal(views(a)[k],views(b)[k]) for k in ['weights','gradients','moment1','moment2'])
assert thread_identity
cp=ROOT/'results/validation-state.npz';checkpoint(a,cp)
c=make();restore(c,cp)
a.run(x.ravel().tolist(),y.ravel().tolist(),steps=1);c.run(x.ravel().tolist(),y.ravel().tolist(),steps=1)
resume_identity=all(np.array_equal(views(a)[k],views(c)[k]) for k in ['weights','gradients','moment1','moment2'])
assert resume_identity
for o in [a,b,c]:o.close()
refusal=False
try:NativeDecoder(LIB,s,max_memory_bytes=1)
except ValueError:refusal=True
assert refusal
write_json(ROOT/'results/validation.json',dict(forward_max_abs_error=err,causal_prefix_exact=causal,
    finite_difference_probes=probes,thread_state_identity=thread_identity,resume_identity=resume_identity,
    insufficient_memory_refused=refusal,reference='BCIR dependency-free scalar decoder, distinct forward implementation; finite differences for VJP'))
print('PASS forward, causal-prefix, 11 parameter-span finite differences, thread state, full-state resume, capacity refusal')
