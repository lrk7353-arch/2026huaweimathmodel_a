"""Check exported plans against their successful official evaluation records."""
import argparse
from common_run import *


def ordered_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def main(out, full_dag=False):
    out=Path(out).resolve()
    with (out/'全部成绩.csv').open(encoding='utf-8-sig') as stream:rows=list(csv.DictReader(stream))
    expected={(f'case_{i:03d}',p,n) for i in range(1,101) for p in (1,2,3) for n in range(1,6)}
    observed={(r['case'],int(r['problem']),int(r['cores'])) for r in rows}
    if len(rows)!=1500 or observed!=expected:raise ValueError('Incorrect plan coverage')
    changed=[];graphs={};dag_count=0
    for row in rows:
        case,p,n=row['case'],int(row['problem']),int(row['cores'])
        record=read_json(row['official_record']);plan=read_json(out/row['plan'])
        if record['status']!='success' or key(record)!=(case,p,n):raise ValueError('Official record identity mismatch')
        if score(record)!=(int(row['after']),int(row['after_copy_bytes'])):raise ValueError('Official score mismatch')
        if ordered_json(plan)!=ordered_json(read_json(record['plan_path'])):raise ValueError('Exported plan/order differs from evaluated plan')
        if not Path(record['result_path']).is_file() or record['result_path']!=row['official_result']:raise ValueError('Missing or mismatched official observation')
        previous=(int(row['before']),int(row['before_copy_bytes']))
        if score(record)>previous:raise ValueError('Portfolio objective regressed')
        different=score(record)<previous
        if different:changed.append(dict(case=case,problem=p,cores=n))
        if full_dag or different:
            if case not in graphs:graphs[case]=GraphIR.from_path(DATA/(case+'.json'))
            validate_plan(graphs[case],plan)
            if len(plan['core_schedules'])!=n:raise ValueError('Wrong number of cores')
            dag_count+=1
    result=dict(coverage=1500,official_success_records=1500,exact_ordered_plan_matches=1500,
                objective_nonregressions=1500,dag_validated_plans=dag_count,
                changed_configurations=changed,dag_scope='all' if full_dag else 'changed configurations; unchanged exports match existing official success records')
    atomic_json(out/'检查结果.json',result)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True);parser.add_argument('--full-dag',action='store_true')
    args=parser.parse_args();main(args.out,args.full_dag)
