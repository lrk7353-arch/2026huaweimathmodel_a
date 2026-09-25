"""Export completed cold runs with portable best plans and full call accounting.

Historical absolute paths remain in immutable call records. Only selected plan
paths in the restored summary are relocated; input/plan/source hashes and all
official metrics are preserved. No evaluations are performed by this utility.
"""
import argparse
import copy
import gzip
import io
import json
from pathlib import Path
import tarfile

from common_run import atomic_json, read_json, score
from solver.common import object_digest


def export(roots,out,variant=None):
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    selected={};manifests=[]
    for root in map(Path,roots):
        manifests.append(dict(root=str(root),manifest=read_json(root/'manifest.json')))
        for row in read_json(root/'results.json'):
            s=read_json(Path(row['summary']))
            if variant and s['variant']!=variant:continue
            key=(s['case'],s['problem'],s['cores'])
            if key in selected:raise ValueError(('duplicate cold run',key))
            if not s['complete'] or s['logical_calls']>s['budget'] or len(s['calls'])!=s['logical_calls']:
                raise ValueError(('invalid run accounting',key))
            if not s.get('best_record') or s['best_record']['status']!='success':
                raise ValueError(('missing successful plan',key))
            selected[key]=s
    rows=[];total_calls=0;failures=0
    with tarfile.open(out/'plans.tar.gz','w:gz') as plans,gzip.open(out/'all_calls.jsonl.gz','wt',encoding='utf-8') as calls:
        for key,s in sorted(selected.items()):
            record=s['best_record'];plan=read_json(record['plan_path'])
            assert object_digest(plan)==record['hashes']['plan_sha256'] and len(plan['core_schedules'])==key[2],key
            member=f'plans/p{key[1]}/n{key[2]}/{key[0]}.json'
            data=(json.dumps(plan,ensure_ascii=False,separators=(',',':'))+'\n').encode()
            entry=tarfile.TarInfo(member);entry.size=len(data);plans.addfile(entry,io.BytesIO(data))
            calls.write(json.dumps(s,ensure_ascii=False,separators=(',',':'))+'\n')
            count=sum(c['record']['status']!='success' for c in s['calls'])
            total_calls+=s['logical_calls'];failures+=count
            rows.append(dict(case=key[0],problem=key[1],cores=key[2],variant=s['variant'],
                plan_member=member,best_record=record,budget=s['budget'],logical_calls=s['logical_calls'],
                failed_calls=count,elapsed_seconds=s['elapsed_seconds'],generation_seconds=s.get('generation_seconds',0),
                generation_errors=s.get('generation_errors',[])))
    atomic_json(out/'selected_records.json',rows)
    atomic_json(out/'export_manifest.json',dict(complete=True,configurations=len(rows),logical_calls=total_calls,
        failed_calls=failures,source_runs=manifests,
        scope='cold results only; best plans and all call records; historical intermediate plans remain on original runtime host'))
    print(json.dumps(dict(configurations=len(rows),logical_calls=total_calls,failed_calls=failures)))


def restore(source,out):
    source,out=Path(source),Path(out);out.mkdir(parents=True,exist_ok=False)
    rows=read_json(source/'selected_records.json')
    with tarfile.open(source/'plans.tar.gz','r:gz') as tar:
        expected={r['plan_member']:r for r in rows}
        for member in tar:
            if not member.isfile() or member.name not in expected:raise ValueError('unexpected plan archive member')
            if Path(member.name).is_absolute() or '..' in Path(member.name).parts:
                raise ValueError('invalid relative plan path')
            row=expected.pop(member.name);plan=json.load(tar.extractfile(member))
            if object_digest(plan)!=row['best_record']['hashes']['plan_sha256']:raise ValueError('plan hash mismatch')
            path=out/member.name;atomic_json(path,plan)
            row['local_plan_path']=str(path.resolve())
        if expected:raise ValueError('missing plan archive members')
    by_key={(r['case'],r['problem'],r['cores']):r for r in rows};results=[]
    with gzip.open(source/'all_calls.jsonl.gz','rt',encoding='utf-8') as f:
        for line in f:
            s=json.loads(line);key=(s['case'],s['problem'],s['cores']);row=by_key.pop(key)
            old=copy.deepcopy(s['best_record']);s['best_record']=dict(old,plan_path=row['local_plan_path'])
            s['original_best_record']=old;s['portable_source']=str(source.resolve())
            slot=out/'slots'/key[0]/f'p{key[1]}_n{key[2]}'
            atomic_json(slot/'summary.json',s)
            results.append(dict(case=key[0],problem=key[1],cores=key[2],variant=s['variant'],
                summary=str((slot/'summary.json').resolve()),best=score(s['best_record'])[0],
                calls=s['logical_calls'],elapsed_seconds=s['elapsed_seconds']))
    if by_key:raise ValueError('missing accounting records')
    atomic_json(out/'results.json',results);atomic_json(out/'manifest.json',read_json(source/'export_manifest.json'))
    print(json.dumps(dict(restored=len(results))))


if __name__=='__main__':
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='mode',required=True)
    e=sub.add_parser('export');e.add_argument('--roots',type=Path,nargs='+',required=True)
    e.add_argument('--out',type=Path,required=True);e.add_argument('--variant')
    r=sub.add_parser('restore');r.add_argument('--source',type=Path,required=True);r.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    if a.mode=='export':export(a.roots,a.out,a.variant)
    else:restore(a.source,a.out)
