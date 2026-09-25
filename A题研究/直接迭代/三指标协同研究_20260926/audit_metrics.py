"""Read-only analysis of the frozen 216-arm corpus. No new solver evaluations."""
import csv,gzip,hashlib,json,statistics
from collections import Counter,defaultdict
from pathlib import Path
HERE=Path(__file__).resolve().parent
SOURCE=HERE.parent/'联合整合_20260926/同预算对照/全部调用记录.json.gz'
def write_csv(name,rows):
    if not rows:return
    with (HERE/name).open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
        w.writeheader();w.writerows(rows)
def main():
    raw=json.load(gzip.open(SOURCE,'rt'));unique={}
    for entry in raw:
        if entry['status']!='success':continue
        rec=entry['record'];m=rec['metrics'];d=m['data_movement_bytes'];c=m.get('cache_stats',{})
        identity=(entry['case'],entry['problem'],entry['cores'],rec['cache_key'])
        row=dict(case=entry['case'],problem=entry['problem'],cores=entry['cores'],group=entry['group'],
            first_variant=entry['variant'],first_call=entry['call'],candidate=entry['name'],
            makespan=m['makespan'],added_bytes=d['added_copy_bytes'],scheduled_bytes=d['scheduled_copy_bytes'],
            original_bytes=d['original_graph_copy_bytes'],partition_added_bytes=d['partition_added_copy_bytes'],
            spill_bytes=d['spill_added_copy_bytes'],hit_rate=c.get('hit_rate'),hit_bytes=c.get('hit_bytes',0),
            active_cores=m.get('active_cores'),
            miss_bytes=c.get('miss_bytes'),eligible_bytes=(c.get('hit_bytes',0)+c.get('miss_bytes',0)) if c else None,
            effective_ddr_byte_proxy=d['scheduled_copy_bytes']-c.get('hit_bytes',0),
            cache_key=rec['cache_key'],plan_path=rec['plan_path'],result_path=rec['result_path'])
        assert row['added_bytes']==row['partition_added_bytes']+row['spill_bytes']
        assert row['scheduled_bytes']==row['original_bytes']+row['added_bytes']
        if c and row['eligible_bytes']:assert abs(row['hit_rate']-row['hit_bytes']/row['eligible_bytes'])<1e-12
        if identity in unique:
            old=unique[identity]
            assert all(old[k]==row[k] for k in ('makespan','added_bytes','hit_rate')),identity
        else:unique[identity]=row
    rows=list(unique.values());write_csv('候选三指标.csv',rows)
    groups=defaultdict(list)
    for row in rows:groups[row['case'],row['problem'],row['cores']].append(row)
    comparisons=[];frontier_rows=[]
    for key,rs in sorted(groups.items()):
        best=min(rs,key=lambda r:(r['makespan'],r['added_bytes']))
        low=min(rs,key=lambda r:(r['added_bytes'],r['makespan']))
        near=[r for r in rs if r['makespan']<=best['makespan']*1.02]
        lownear=min(near,key=lambda r:(r['added_bytes'],r['makespan']))
        result=dict(case=key[0],problem=key[1],cores=key[2],candidates=len(rs),
            fastest_time=best['makespan'],fastest_added=best['added_bytes'],fastest_spill=best['spill_bytes'],
            fastest_hit=best['hit_rate'],fastest_name=best['candidate'],
            min_copy_time=low['makespan'],min_copy_added=low['added_bytes'],min_copy_name=low['candidate'],
            min_copy_time_penalty_pct=100*(low['makespan']/best['makespan']-1),
            near2_time=lownear['makespan'],near2_added=lownear['added_bytes'],
            near2_saved_bytes=best['added_bytes']-lownear['added_bytes'])
        if key[1]==3:
            high=min(rs,key=lambda r:(-r['hit_rate'],r['makespan'],r['added_bytes']))
            result.update(max_hit=high['hit_rate'],max_hit_time=high['makespan'],max_hit_added=high['added_bytes'],
                max_hit_name=high['candidate'],max_hit_time_penalty_pct=100*(high['makespan']/best['makespan']-1),
                fastest_eligible=best['eligible_bytes'],max_hit_eligible=high['eligible_bytes'],
                fastest_ddr_proxy=best['effective_ddr_byte_proxy'],max_hit_ddr_proxy=high['effective_ddr_byte_proxy'])
        comparisons.append(result)
        # Exact observed nondominated (time, logical COPY volume) values; not a full-space frontier.
        seen_values=set();best_copy=float('inf')
        for r in sorted(rs,key=lambda r:(r['makespan'],r['added_bytes'])):
            value=r['makespan'],r['added_bytes']
            if value in seen_values:continue
            seen_values.add(value)
            if r['added_bytes']<best_copy:
                frontier_rows.append(r);best_copy=r['added_bytes']
    write_csv('逐配置取舍.csv',comparisons);write_csv('已观测非支配候选.csv',frontier_rows)
    summary=[]
    for p in (1,2,3):
        rs=[r for r in comparisons if r['problem']==p];candidates=[r for r in rows if r['problem']==p]
        s=dict(problem=p,configs=len(rs),unique_candidates=len(candidates),
            configs_min_copy_slower=sum(r['min_copy_time']>r['fastest_time'] for r in rs),
            configs_min_copy_slower_over5pct=sum(r['min_copy_time_penalty_pct']>5 for r in rs),
            configs_with_lower_copy_within2pct=sum(r['near2_saved_bytes']>0 for r in rs),
            configs_fastest_has_spill=sum(r['fastest_spill']>0 for r in rs),
            configs_observed_frontier_multiple=sum(sum(x['case']==r['case'] and x['problem']==p and x['cores']==r['cores'] for x in frontier_rows)>1 for r in rs))
        if p==3:
            s.update(configs_max_hit_slower=sum(r['max_hit_time']>r['fastest_time'] for r in rs),
                     configs_max_hit_slower_over5pct=sum(r['max_hit_time_penalty_pct']>5 for r in rs),
                     configs_max_hit_increases_logical_copy=sum(r['max_hit_added']>r['fastest_added'] for r in rs))
        summary.append(s)
    result=dict(scope='Retrospective selected 24-graph/108-config B16 panel; not all 100 graphs, not causal evidence.',
        source_sha256=hashlib.sha256(SOURCE.read_bytes()).hexdigest(),total_calls=len(raw),successful_calls=sum(x['status']=='success' for x in raw),
        unique_successful_candidates=len(rows),official_calls=0,summary=summary)
    (HERE/'指标审计摘要.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    print('Representative rows')
    for r in comparisons:
        if r['cores']==5 and r['case'] in ('case_009','case_017','case_044','case_046','case_064','case_069','case_075','case_094'):
            print(json.dumps(r,ensure_ascii=False))
if __name__=='__main__':main()
