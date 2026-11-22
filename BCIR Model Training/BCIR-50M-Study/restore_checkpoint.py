"""Rebuild a full-state study NPZ from independently hashed weights/optimizer parts."""
import argparse,json,hashlib
from pathlib import Path
import numpy as np

def restore_parts(parts,output):
    parts=Path(parts);manifest=json.loads((parts/'checkpoint-parts.json').read_text())
    arrays={}
    for name,record in manifest['arrays'].items():
        a=np.load(parts/record['file'],mmap_mode='r',allow_pickle=False)
        if list(a.shape)!=record['shape'] or str(a.dtype)!=record['dtype']:raise ValueError('array layout '+name)
        if hashlib.sha256(memoryview(a).cast('B')).hexdigest()!=record['sha256']:raise ValueError('array hash '+name)
        arrays[name]=a
    target=Path(output);target.parent.mkdir(parents=True,exist_ok=True)
    with target.open('wb') as f:np.savez(f,metadata=json.dumps(manifest['metadata']),**arrays)
    print(json.dumps(dict(output=str(target),bytes=target.stat().st_size,step=manifest['metadata']['step'],verified_arrays=list(arrays))))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--parts',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();restore_parts(a.parts,a.output)
