"""Restore lossless history blobs into a NEW directory without changing workspaces."""
import argparse,tarfile,json,gzip,hashlib,shutil
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--assets',type=Path,required=True);p.add_argument('--out',type=Path,required=True);p.add_argument('--source',type=int,choices=[0,1]);a=p.parse_args()
a.out.mkdir(parents=True,exist_ok=False);blob=a.out/'_blobs';blob.mkdir()
with gzip.open(a.assets/'history-file-index.jsonl.gz','rt',encoding='utf-8') as f:rows=[json.loads(s) for s in f if s.strip()]
if a.source is not None:rows=[r for r in rows if r['source']==a.source]
wanted={r['sha256'] for r in rows}
for part in sorted({r['part'] for r in rows}):
 with tarfile.open(a.assets/f'history-blobs-{part:03}.tar.gz') as t:
  for m in t:
   h=Path(m.name).name
   if h in wanted:
    b=t.extractfile(m).read();assert hashlib.sha256(b).hexdigest()==h;(blob/h).write_bytes(b)
for r in rows:
 rel=Path(r['path']);assert not rel.is_absolute() and '..' not in rel.parts
 dst=a.out/f'source{r["source"]}'/rel;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(blob/r['sha256'],dst)
print('restored',len(rows),'files; intermediate blobs retained in',blob)
