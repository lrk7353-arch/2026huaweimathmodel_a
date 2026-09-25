"""Compare complete method groups from completed runs; no new evaluations."""
import argparse
from common_run import *
from analyze_quality_panel import analyze


def combine(group,out):
    runs=R/'直接迭代/运行结果'
    names=('综合回归_v1','综合粒度回归_v1') if group=='small' else ('综合大图_v1','综合粒度大图_v1')
    first,second=[read_json(runs/n/'summary.json') for n in names]
    protocols=[read_json(runs/n/'plan.json') for n in names]
    assert all(s['complete'] for s in (first,second))
    assert protocols[0]['cases']==protocols[1]['cases'] and protocols[0]['seeds']==protocols[1]['seeds']
    rows=first['rows']+[r for r in second['rows'] if r['method']=='portfolio_diverse']
    methods=['adaptive','hybrid','portfolio','portfolio_diverse']
    cases=protocols[0]['cases'];seeds=protocols[0]['seeds']
    expected={(c,m,s) for c in cases for m in methods for s in seeds}
    assert len(rows)==len(expected) and {(r['case'],r['method'],r['seed']) for r in rows}==expected
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    atomic_json(out/'plan.json',dict(cases=cases,methods=methods,seeds=seeds,reference='portfolio_diverse',jobs=sorted(expected),
        source_runs=names,logical_cap=12,case_soft_seconds=protocols[0]['case_soft_seconds'],
        scope='derived comparison; select complete method groups, never best seed or best run; source walltimes remain separate'))
    atomic_json(out/'summary.json',dict(complete=True,completed=len(rows),expected=len(rows),rows=rows,wall_seconds=None))
    write_csv(out/'results.csv',rows)
    return analyze(out)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--group',choices=['small','large'],required=True);p.add_argument('--out',required=True)
    a=p.parse_args();combine(a.group,a.out)
