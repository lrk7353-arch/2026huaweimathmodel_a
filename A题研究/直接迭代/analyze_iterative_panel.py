"""Report every paired cell and apply the predeclared promotion/extension rules."""
import argparse
import statistics
from common_run import *
from run_iterative_panel import extension_signal


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    a=p.parse_args()
    source=read_json(a.run/'summary.json')
    protocol=read_json(a.run/'protocol.json')
    if not source['completed']:
        p.error('incomplete panel cannot receive a promotion decision')
    records={(x['case'],x['cores'],x['method']):x for x in source['rows']}
    pairs, trials, trial_lists=[],[],{}
    for job in protocol['jobs']:
        case,n=job['case'],job['cores']
        old,new=records[case,n,'single'],records[case,n,'iterative']
        assert old['status']==new['status']=='success'
        delta=job['original_singlecore']/new['after']-job['original_singlecore']/old['after']
        pct=100*(old['after']-new['after'])/old['after']
        pairs.append(dict(case=case,cores=n,baseline=old['before'],single=old['after'],
            iterative=new['after'],time_reduction_pct=pct,speedup_delta=delta,
            time_result='win' if pct>0 else 'loss' if pct<0 else 'tie',
            single_calls=old['logical_calls'],iterative_calls=new['logical_calls'],
            single_added_copy=old['after_copy_bytes'],iterative_added_copy=new['after_copy_bytes']))
        for method,row in [('single',old),('iterative',new)]:
            summary=read_json(a.run/case/f'n{n}'/method/'summary.json')
            assert 1+len(summary['evaluations'])==row['logical_calls']<=protocol['call_budget']
            assert row['candidate_new_calls']==len(summary['evaluations'])
            if method=='iterative':trial_lists[case,n]=summary['evaluations']
            best=job['expected_baseline']
            for i,t in enumerate(summary['evaluations'],2):
                rec=t['record']; success=rec['status']=='success'
                value=score(rec)[0] if success else None
                if success:best=min(best,value)
                trials.append(dict(case=case,cores=n,method=method,logical_call=i,
                    generation=t['generation'],candidate=t['name'],parent_time=t['parent_score'][0],
                    status=rec['status'],time=value,best_time_so_far=best,
                    added_copy=score(rec)[1] if success else None,accepted=t['accepted'],
                    lower_bound=t['metadata']['lower_bound'],cache_hit=rec['cache_hit']))
    groups=[]
    for n in (2,3,4,5):
        group=[x for x in pairs if x['cores']==n]
        groups.append(dict(cores=n,cells=len(group),wins=sum(x['time_result']=='win' for x in group),
            ties=sum(x['time_result']=='tie' for x in group),losses=sum(x['time_result']=='loss' for x in group),
            mean_time_reduction_pct=statistics.mean(x['time_reduction_pct'] for x in group),
            mean_speedup_delta=statistics.mean(x['speedup_delta'] for x in group)))
    errors=sum(x['errors'] for x in source['rows']);timeouts=sum(x['timeouts'] for x in source['rows'])
    winning_graphs=sorted({x['case'] for x in pairs if x['time_result']=='win'})
    limits=protocol['promotion']
    checks=dict(two_distinct_winning_graphs=len(winning_graphs)>=limits['min_distinct_winning_graphs'],
        nonnegative_each_core=all(x['mean_time_reduction_pct']>=limits['per_core_mean_time_reduction_min'] for x in groups),
        positive_mean_speedup_delta=statistics.mean(x['speedup_delta'] for x in pairs)>0,
        no_large_regression=min(x['time_reduction_pct'] for x in pairs)>=-limits['max_regression_pct'],
        no_errors=errors==0,no_timeouts=timeouts==0)
    decision=dict(promotion_to_from_scratch_pilot=all(checks.values()),checks=checks,
        extend_to_budget12=extension_signal(source['rows'],trial_lists),winning_graphs=winning_graphs,
        wins=sum(x['time_result']=='win' for x in pairs),ties=sum(x['time_result']=='tie' for x in pairs),
        losses=sum(x['time_result']=='loss' for x in pairs),
        mean_time_reduction_pct=statistics.mean(x['time_reduction_pct'] for x in pairs),
        mean_speedup_delta=statistics.mean(x['speedup_delta'] for x in pairs),
        errors=errors,timeouts=timeouts,logical_calls=source['logical_calls'],
        actual_new_calls=source['actual_new_calls'],elapsed_seconds=source['elapsed_seconds'],
        scope='engineering screening on 12 warm-start cells, not proof of universal superiority')
    a.out.mkdir(parents=True,exist_ok=True)
    write_csv(a.out/'逐配置对照.csv',pairs)
    write_csv(a.out/'各核数对照.csv',groups)
    write_csv(a.out/'全部候选记录.csv',trials)
    atomic_json(a.out/'晋级判定.json',decision)
    print(json.dumps(decision,ensure_ascii=False))


if __name__=='__main__':main()
