from common import *
import subprocess,platform

if not LIB.exists():
    t=time.monotonic();NativeDecoder.build(ROOT/'build',cc='gcc')
    write_json(ROOT/'results/build.json',dict(seconds=time.monotonic()-t,library_sha256=sha(LIB),
        compiler=subprocess.check_output(['gcc','--version'],text=True).splitlines()[0],
        flags='NativeDecoder.build: C11 -O3 -ffp-contract=off -fno-math-errno; current production trainer/tensor units',
        sources={p:sha(REPO/'runtime/c'/p) for p in ['bcir_tensor.c','bcir_tensor.h','bcir_decoder_train.c','bcir_decoder_train.h']}))

data=corpus()
write_json(ROOT/'results/data.json',{k:dict(bytes=int(v.size),encoded_sha256=sha(ROOT/'data'/f'{k}.npy'),
    parquet_sha256=sha(REPO/'docs/research/gp-global/data'/f'wikitext2-{k}.parquet')) for k,v in data.items()})
results={}
for name in ARCH:
    t=time.monotonic();m=model(name);init=time.monotonic()-t
    x,y=batches(data['train'],8911,7)
    m.run(x[:2].ravel().tolist(),y[:2].ravel().tolist(),steps=2)
    times=[]
    for xx,yy in zip(x[2:],y[2:]):
        t=time.monotonic();ev=m.run(xx.ravel().tolist(),yy.ravel().tolist(),steps=1)
        times.append(time.monotonic()-t)
    results[name]=dict(parameters=count(name),init_s=init,memory_bytes=m.memory_bytes,
        step_seconds=times,median_step_seconds=float(np.median(times)),rss_max_mib=rss_mib(),
        last_loss=ev[-1]['loss'],spec=asdict(m.spec),contract=CONTRACT)
    m.close();del m
    write_json(ROOT/'results/benchmark.json',results)
    print(name,json.dumps(results[name]),flush=True)
