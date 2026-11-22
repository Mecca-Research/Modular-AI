"""Standalone study; BCIR source is imported at the pinned checkout, never edited."""
import os
os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')
os.environ.setdefault('OMP_NUM_THREADS', '1')
import sys, json, hashlib, time, ctypes as ct, resource
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent / 'BCIR'
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(ROOT / 'deps'))
from bcir.hosted.models.native import NativeDecoder, NativeAdamW, parameter_layout
from bcir.frontends.models.decode import DecoderSpec
from dataclasses import asdict

LIB = ROOT / 'build/bcir_decoder_train.so'
CONTRACT = 'kblock+reduce+attn'
SEEDS = [20261104, 20261105]
ARCH = {
    'pilot1m': dict(d_model=192,n_heads=4,n_kv_heads=2,n_layers=2,d_ff=512),
    'reference10m': dict(d_model=384,n_heads=8,n_kv_heads=4,n_layers=6,d_ff=1024),
    'wide10m': dict(d_model=512,n_heads=8,n_kv_heads=2,n_layers=4,d_ff=1152),
    'wide50m': dict(d_model=768,n_heads=12,n_kv_heads=4,n_layers=8,d_ff=2048),
    'deep50m': dict(d_model=640,n_heads=10,n_kv_heads=2,n_layers=12,d_ff=1664),
}

def spec(name):
    return DecoderSpec(vocab_size=260, activation='silu_gate', tied_embeddings=True, **ARCH[name])

def count(name):
    return sum(row[3] for row in parameter_layout(spec(name)))

def model(name,seed=SEEDS[0],batch=4,context=64,threads=2,contract=CONTRACT):
    return NativeDecoder(LIB,spec(name),seed=seed,batch_size=batch,context_length=context,
                         threads=threads,contract=contract,max_memory_bytes=4*1024**3)

def views(m):
    return {k:np.ctypeslib.as_array(v) for k,v in m.buffers.items()}

def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        while chunk:=f.read(8*1024**2):h.update(chunk)
    return h.hexdigest()

def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(value,indent=2,allow_nan=False)+'\n');temp.replace(path)

def checkpoint(m,path,full=True):
    v=views(m)
    keys=['weights','moment1','moment2'] if full else ['weights']
    for k in keys:
        if not np.isfinite(v[k]).all(): raise ValueError('nonfinite checkpoint '+k)
    meta=dict(schema='bcir.study.native-arrays.v1',decoder=asdict(m.spec),contract=m.contract,
        batch_size=m.batch_size,context_length=m.context_length,step=int(m.state.step),
        beta1_power=float(m.state.beta1_power),beta2_power=float(m.state.beta2_power),full=full)
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.with_suffix('.tmp').open('wb') as f:np.savez(f,metadata=json.dumps(meta),**{k:v[k] for k in keys})
    path.with_suffix('.tmp').replace(path)
    return {'path':str(path.relative_to(ROOT)),'sha256':sha(path),'bytes':path.stat().st_size,**meta}

def restore(m,path,full=True):
    with np.load(path,allow_pickle=False) as f:
        meta=json.loads(str(f['metadata']))
        if meta['decoder']!=asdict(m.spec) or meta['contract']!=m.contract:raise ValueError('checkpoint identity mismatch')
        for k in ['weights','moment1','moment2'] if full else ['weights']:
            a=f[k]
            if a.shape!=views(m)[k].shape or a.dtype!=np.float32 or not np.isfinite(a).all():raise ValueError('invalid array')
            views(m)[k][:]=a
        if full:
            m.state.step=meta['step'];m.state.beta1_power=meta['beta1_power'];m.state.beta2_power=meta['beta2_power']
    return meta

def corpus():
    """Original retained WikiText-2 parquet splits, byte encoding unchanged."""
    import pyarrow.parquet as pq
    out={}
    for split in ['train','validation','test']:
        path=REPO/'docs/research/gp-global/data'/f'wikitext2-{split}.parquet'
        cache=ROOT/'data'/f'{split}.npy'
        if not cache.exists():
            text='\n'.join(pq.read_table(path,columns=['text'])['text'].to_pylist())
            np.save(cache,np.frombuffer(text.encode('utf-8'),dtype=np.uint8).astype(np.uint32))
        out[split]=np.load(cache,mmap_mode='r')
    return out

def batches(data,seed,n,batch=4,context=64):
    rng=np.random.default_rng(seed)
    starts=rng.integers(0,len(data)-context-1,size=(n,batch))
    rows=data[starts[:,:,None]+np.arange(context+1)]
    return np.ascontiguousarray(rows[:,:,:-1],dtype=np.uint32),np.ascontiguousarray(rows[:,:,1:],dtype=np.uint32)

def forward(m,x):
    x=np.ascontiguousarray(x,dtype=np.uint32)
    rc=m.lib.bcir_decoder_forward(ct.byref(m.state),x.ctypes.data_as(ct.POINTER(ct.c_uint32)),x.size)
    if rc:raise ValueError('forward status '+str(rc))
    n=m.batch_size*m.context_length*m.spec.vocab_size
    return views(m)['activations'][m.activation_count-n:].reshape(m.batch_size,m.context_length,m.spec.vocab_size).copy()

def evaluate(m,xy):
    x,y=xy; ce=[];correct=0;n=0
    for xx,yy in zip(x,y):
        z=forward(m,xx).astype(np.float64)
        z-=z.max(axis=-1,keepdims=True)
        loss=np.log(np.exp(z).sum(axis=-1))-np.take_along_axis(z,yy[...,None],axis=-1)[...,0]
        ce.append(loss);correct+=int((z.argmax(-1)==yy).sum());n+=yy.size
    return dict(ce=float(np.mean(ce)),accuracy=correct/n,tokens=n,
                batch_ce=[float(a.mean()) for a in ce])

def rss_mib():return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
