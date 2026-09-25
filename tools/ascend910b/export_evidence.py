#!/usr/bin/env python3
"""Export an allowlist of our experiment artifacts; no platform or SSH files."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile


def main():
    p=argparse.ArgumentParser();p.add_argument('workspace',type=Path);p.add_argument('output',type=Path)
    args=p.parse_args();root=args.workspace;out=args.output
    out.mkdir(parents=True,exist_ok=False)
    for name in ['full_01','smoke_01','confirm_01','sharing_01','sharing_confirm',
                 'profile_evidence','sharing_profile_evidence','figures_final']:
        shutil.copytree(root/'mechanisms'/name,out/name)
    for name in ['mixed_01','mixed_graph_01','mixed_graph_smoke','mixed_profile_evidence']:
        shutil.copytree(root/name,out/name)
    shutil.copy2(root/'device_attributes_v2.json',out/'device_attributes.json')
    (out/'source_snapshots').mkdir()
    for old,new in [('mechanism_v1_source.tar.gz','initial.tar.gz'),('v2-src.tar.gz','expanded.tar.gz')]:
        shutil.copy2(root/old,out/'source_snapshots'/new)
    with (out/'device_end.txt').open('w') as f:
        subprocess.run(['npu-smi','info'],stdout=f,stderr=subprocess.STDOUT,check=True,timeout=15)
    subprocess.run(['python3',str(root/'mechanisms/plot_evidence.py'),str(out)],check=True,timeout=60)
    counts={};total=0
    for batch in ['full_01','confirm_01','sharing_01','sharing_confirm','mixed_01','mixed_graph_01']:
        files=list((out/batch).glob('*_raw.csv')) or [out/batch/'raw.csv']
        size=0;configs=set()
        for file in files:
            for row in csv.DictReader(file.open()):
                if 'max_abs_error' in row: assert float(row['max_abs_error'])==0
                if 'correct' in row: assert row['correct']=='True'
                key=row.get('id') or (row['size'],row['elements'],row['mode'])
                configs.add(key);size+=1
        counts[batch]={'configuration_batches':len(configs),'timed_sequences':size,'all_correct':True}
        total+=size
    manifest={'formal_batches':counts,'formal_timed_sequences':total,
              'smoke_samples_excluded':36,'note':'One measured sequence can launch many kernels; profiler runs are separate.',
              'files':{str(f.relative_to(out)):{'bytes':f.stat().st_size,'sha256':hashlib.sha256(f.read_bytes()).hexdigest()}
                       for f in sorted(out.rglob('*')) if f.is_file()}}
    (out/'artifact_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    with tarfile.open(out.with_suffix('.tar.gz'),'w:gz') as tar:
        tar.add(out,arcname='mechanism_evidence')
    print(json.dumps(counts,indent=2));print('TOTAL',total)


if __name__=='__main__':main()
