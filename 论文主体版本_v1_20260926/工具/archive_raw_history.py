"""Lossless content-deduplicated history archive; no experiments are executed."""
import os,sys,json,hashlib,gzip,tarfile,io,time
from pathlib import Path
out=Path(sys.argv[1]);out.mkdir(parents=True,exist_ok=True)
roots=[Path('/Users/liyu/Desktop/中文题目/A题研究'),Path('/Users/liyu/Desktop/A题P1最新审阅/A题研究')]
seen={};part=0;size=0;count=0;archive=None;start=time.time();parts=[]
def newpart():
 global archive,part,size
 if archive:archive.close()
 part+=1;size=0;n=f'history-blobs-{part:03}.tar.gz';parts.append(n);archive=tarfile.open(out/n,'w:gz',compresslevel=1)
newpart()
with gzip.open(out/'history-file-index.jsonl.gz','wt',encoding='utf-8',compresslevel=1) as index:
 for ri,root in enumerate(roots):
  for directory,dirs,files in os.walk(root):
   dirs[:]=sorted(d for d in dirs if d not in ('.venv','__pycache__','.git','node_modules'))
   for name in sorted(files):
    if name=='.DS_Store' or name.endswith(('.pyc','.lock')):continue
    p=Path(directory)/name
    if p.is_symlink():continue
    b=p.read_bytes();h=hashlib.sha256(b).hexdigest()
    if h not in seen:
     if size+len(b)>450*1024**2 and size:newpart()
     n='blobs/'+h;ti=tarfile.TarInfo(n);ti.size=len(b);ti.mtime=0;archive.addfile(ti,io.BytesIO(b));size+=len(b);seen[h]=part
    index.write(json.dumps({'source':ri,'path':str(p.relative_to(root)),'size':len(b),'sha256':h,'part':seen[h]},ensure_ascii=False)+'\n');count+=1
    if count%20000==0:print('files',count,'unique',len(seen),'parts',part,'seconds',round(time.time()-start),flush=True)
archive.close()
(out/'history-manifest.json').write_text(json.dumps({'source_roots':[str(x) for x in roots],'files':count,'unique_files':len(seen),'parts':parts,'excluded':['virtual environments','compiled Python cache','Git internals','node_modules','.DS_Store','lock files'],'seconds':time.time()-start},ensure_ascii=False,indent=2))
print('DONE',count,len(seen),len(parts),flush=True)
