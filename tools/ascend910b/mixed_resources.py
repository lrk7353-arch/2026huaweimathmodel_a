#!/usr/bin/env python3
"""Framework-level matrix/vector overlap; device envelopes, not a synthetic P1 score."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import random
import time
import torch
import torch_npu


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--samples',type=int,default=30)
    p.add_argument('--warmup',type=int,default=10)
    args=p.parse_args()
    args.out.mkdir(parents=True,exist_ok=False)
    if torch.npu.device_count()!=1:
        raise RuntimeError('Expected one assigned device')
    torch.npu.set_device(0)
    control=torch.npu.current_stream()
    cube,vector=torch.npu.Stream(),torch.npu.Stream()
    start=torch.npu.Event(enable_timing=True)
    stop=torch.npu.Event(enable_timing=True)
    dc,dv=torch.npu.Event(),torch.npu.Event()
    configs=[]
    for size in [512,1024,2048]:
        for elements in [1<<20,1<<24]:
            for mode in ['matrix_only','vector_only','serial','parallel']:
                configs.append(dict(size=size,elements=elements,mode=mode,repeats=16))
    manifest={'scope':'installed torch_npu matrix/vector scheduling; framework-selected kernels',
              'samples':args.samples,'warmup':args.warmup,'configs':configs,
              'torch':torch.__version__,'torch_npu':torch_npu.__version__,
              'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (args.out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    with (args.out/'raw.csv').open('w',newline='') as f:
        fields=['size','elements','mode','repeats','sample','device_envelope_us','host_us','correct']
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader()
        for size in [512,1024,2048]:
            a=torch.full((size,size),0.125,device='npu',dtype=torch.float16)
            b=torch.full_like(a,0.125); c=torch.empty_like(a)
            for elements in [1<<20,1<<24]:
                x=torch.full((elements,),0.25,device='npu',dtype=torch.float32)
                y=torch.full_like(x,0.5); z=torch.empty_like(x)
                torch.npu.synchronize()

                def run(mode):
                    torch.npu.synchronize()
                    t=time.perf_counter_ns()
                    start.record(control)
                    if mode=='parallel':
                        cube.wait_event(start); vector.wait_event(start)
                        with torch.npu.stream(cube):
                            for _ in range(16): torch.mm(a,b,out=c)
                            dc.record(cube)
                        with torch.npu.stream(vector):
                            for _ in range(16): torch.add(x,y,out=z)
                            dv.record(vector)
                        control.wait_event(dc); control.wait_event(dv)
                    else:
                        with torch.npu.stream(control):
                            if mode!='vector_only':
                                for _ in range(16): torch.mm(a,b,out=c)
                            if mode!='matrix_only':
                                for _ in range(16): torch.add(x,y,out=z)
                    stop.record(control); stop.synchronize()
                    host=(time.perf_counter_ns()-t)/1000
                    device=start.elapsed_time(stop)*1000
                    correct=True
                    if mode!='vector_only': correct &= bool((c==size/64).all().item())
                    if mode!='matrix_only': correct &= bool((z==0.75).all().item())
                    if not correct: raise RuntimeError('Numeric validation failed')
                    return device,host,correct

                modes=['matrix_only','vector_only','serial','parallel']
                for mode in modes:
                    for _ in range(args.warmup): run(mode)
                rng=random.Random(20260926+size+elements)
                for sample in range(args.samples):
                    rng.shuffle(modes)
                    for mode in modes:
                        device,host,correct=run(mode)
                        w.writerow(dict(size=size,elements=elements,mode=mode,repeats=16,sample=sample,
                                        device_envelope_us=device,host_us=host,correct=correct));f.flush()
                print(f'completed matrix={size} vector={elements}',flush=True)
                del x,y,z
            del a,b,c


if __name__=='__main__':
    main()
