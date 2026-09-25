"""Export a portable best-plan portfolio against the recorded start of this round."""
import argparse,shutil,statistics
from collections import Counter
from common_run import *

def main(a):
    out=Path(a.out).resolve();out.mkdir(parents=True,exist_ok=False)
    before={key(r):r for r in read_json(a.before)['records']};after=known();rows=[]
    baselines={f'case_{i:03d}':read_json(R/f'solver/runs/full_initial_v1/results/case_{i:03d}/singlecore.json')['metrics']['makespan'] for i in range(1,101)}
    for k,r in sorted(after.items()):
        case,p,n=k;b=before.get(k)
        assert r['status']=='success'
        target=out/'方案'/f'p{p}'/f'n{n}'/(case+'_multicore_res.json');target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(r['plan_path'],target)
        rows.append(dict(case=case,problem=p,cores=n,before=score(b)[0] if b else None,after=score(r)[0],
                         before_copy_bytes=score(b)[1] if b else None,after_copy_bytes=score(r)[1],
                         reduction_pct=100*(1-score(r)[0]/score(b)[0]) if b else None,
                         original_singlecore=baselines[case],plan=str(target.relative_to(out)),
                         official_record=r['record_path'],official_result=r['result_path']))
    write_csv(out/'全部成绩.csv',rows)
    improved=[r for r in rows if r['before'] is not None and (r['after'],r['after_copy_bytes'])<(r['before'],r['before_copy_bytes'])]
    added=[r for r in rows if r['before'] is None]
    write_csv(out/'改善清单.csv',improved);write_csv(out/'补齐清单.csv',added)
    groups=[]
    for p in (1,2,3):
        for n in (2,3,4,5):
            rr=[r for r in rows if r['problem']==p and r['cores']==n]
            assert len(rr)==100 and all(r['before'] is not None for r in rr)
            groups.append(dict(problem=p,cores=n,cases=len(rr),
                before_mean_speedup=statistics.mean(r['original_singlecore']/r['before'] for r in rr),
                after_mean_speedup=statistics.mean(r['original_singlecore']/r['after'] for r in rr),
                mean_paired_time_reduction_pct=statistics.mean(r['reduction_pct'] for r in rr),
                strict_time_improved=sum(r['after']<r['before'] for r in rr)))
    write_csv(out/'各核数汇总.csv',groups)
    missing=[(f'case_{c:03d}',p,n) for c in range(1,101) for p in (1,2,3) for n in range(1,6) if (f'case_{c:03d}',p,n) not in after]
    s=dict(coverage=len(rows),previous_coverage=len(before),new_coverage=len(added),missing=missing,
           objective_improved=len(improved),strict_time_improved=sum(r['after']<r['before'] for r in improved),
           regressions=sum(r['before'] is not None and r['after']>r['before'] for r in rows),groups=groups,
           coverage_by_cores=dict(Counter(r['cores'] for r in rows)),
           scope='cumulative official-evaluated best plans; added budget varies; not equal-budget algorithm ranking')
    atomic_json(out/'summary.json',s)
    print(json.dumps(s,ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--before',required=True);p.add_argument('--out',required=True);main(p.parse_args())
