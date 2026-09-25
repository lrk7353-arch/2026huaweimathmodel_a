"""Independently replay new cumulative wins and export all positive/negative evidence."""
import argparse
import csv
import gzip
import hashlib
import json
import shutil
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from common_run import *

ROOT=R.parent
HERE=Path(__file__).resolve().parent
PUBLIC=HERE/'闭环验证_20260925/下一阶段攻坚/重划实证_v1'


def replay(job):
    case,cores,plan,expected,out=job
    rec=run_candidate(case,1,cores,read_json(plan),Path(out)/case/f'n{cores}',60)
    if rec['status']!='success' or score(rec)[0]!=expected:
        raise AssertionError('independent replay mismatch: '+case)
    return (case,cores),rec


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--dev',type=Path,required=True)
    parser.add_argument('--transfer',type=Path,required=True);parser.add_argument('--replay-out',type=Path,required=True)
    args=parser.parse_args();dev=read_json(args.dev/'summary.json');transfer=read_json(args.transfer/'summary.json')
    replayout=args.replay_out.resolve()
    if replayout.exists():raise ValueError('fresh replay directory required')
    replayout.mkdir(parents=True)
    cumulative=list(csv.DictReader((HERE/'闭环验证_20260925/累计1500配置成绩.csv').open(encoding='utf-8-sig')))
    lookup={(x['case'],int(x['problem']),int(x['cores'])):x for x in cumulative}
    proposals={};attempts=[];devrows=[];time_starved=[]
    for cell in dev['cells']:
        case=cell['case'];cores=cell['cores']
        devrows.append(dict(case=case,cores=cores,before=cell['makespan'],after=cell['selected_makespan'],
                            reduction=1-cell['selected_makespan']/cell['makespan'],selected=cell['selected'],
                            calls=cell['calls'],elapsed_seconds=cell['elapsed_seconds']))
        for entry in read_json(args.dev/case/'attempts.json'):
            rec=entry['record'];plan=read_json(entry['plan_path']);sizes=Counter(plan['node_to_subgraph'].values())
            attempts.append(dict(stage='development',case=case,cores=cores,name=entry['name'],phase='',
                status=rec['status'],makespan=rec.get('metrics',{}).get('makespan'),
                added_copy=rec.get('metrics',{}).get('data_movement_bytes',{}).get('added_copy_bytes'),
                tasks=len(sizes),tiny_tasks=sum(s<=8 for s in sizes.values()),
                graph_sha256=rec['hashes']['graph_sha256'],plan_sha256=rec['hashes']['plan_sha256'],
                result_sha256=rec.get('result_sha256'),record_path=rec['record_path'],
                elapsed_seconds=rec['elapsed_seconds'],cache_hit=rec['cache_hit']))
        if cell['selected_makespan']<cell['makespan']:
            proposals[case,cores]=dict(plan=cell['selected_plan'],record=read_json(cell['selected_record']),source='本轮开发重划')
    for row in transfer['rows']:
        s=read_json(row['summary'])
        row['repartition_calls']=sum(e['phase']=='bottleneck_repartition' for e in s['evaluations'])
        row['adaptive_calls']=sum(e['phase']=='bottleneck_adaptive' for e in s['evaluations'])
        if row['method']=='new' and s['stop_reason']=='time_budget' and not any(e['stage']=='bottleneck_generation' for e in s['routing']):
            time_starved.append({'case':row['case'],'cores':row['cores'],'prefix_calls':s['prefix_calls']})
        for entry in s['evaluations']:
            rec=entry['record']
            attempts.append(dict(stage='transfer_'+row['method'],case=row['case'],cores=row['cores'],name=entry['name'],
                phase=entry['phase'],status=rec['status'],makespan=rec.get('metrics',{}).get('makespan'),
                added_copy=rec.get('metrics',{}).get('data_movement_bytes',{}).get('added_copy_bytes'),
                graph_sha256=rec['hashes']['graph_sha256'],plan_sha256=rec['hashes'].get('plan_sha256'),
                result_sha256=rec.get('result_sha256'),record_path=rec['record_path'],
                elapsed_seconds=rec['elapsed_seconds'],cache_hit=rec['cache_hit']))
        key=row['case'],row['cores'];old=int(lookup[row['case'],1,row['cores']]['makespan'])
        if row['makespan'] is not None and row['makespan']<old and (key not in proposals or row['makespan']<score(proposals[key]['record'])[0]):
            proposals[key]=dict(plan=s['best_record']['plan_path'],record=s['best_record'],source='本轮从头_'+row['method'])
    jobs=[(case,n,x['plan'],score(x['record'])[0],str(replayout)) for (case,n),x in sorted(proposals.items())]
    with ProcessPoolExecutor(max_workers=2) as pool: replayed=dict(pool.map(replay,jobs))
    portable=[]
    for (case,n),proposal in sorted(proposals.items()):
        replay_record=replayed[case,n];row=lookup[case,1,n];before=int(row['makespan'])
        destination=PUBLIC/'精选方案'/f'{case}_p1_n{n}.json'
        destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(replay_record['plan_path'],destination)
        if hashlib.sha256(destination.read_bytes()).hexdigest()!=replay_record['hashes']['plan_sha256']:
            raise AssertionError('portable plan bytes differ from independently replayed plan')
        recordpath=PUBLIC/'复评记录'/f'{case}_p1_n{n}.json'
        atomic_json(recordpath,dict(record=replay_record,raw_result_storage='local my_runs, intentionally not tracked; re-run official evaluator to reproduce',
                   portable_plan=str(destination.relative_to(ROOT)),original_selection_source=proposal['source']))
        row.update(makespan=score(replay_record)[0],added_copy=score(replay_record)[1],source=proposal['source'],
                   plan=str(destination.relative_to(ROOT)),speedup=float(row['original_singlecore'])/score(replay_record)[0])
        portable.append(dict(case=case,cores=n,before=before,after=score(replay_record)[0],
                             source=proposal['source'],plan=row['plan'],record=str(recordpath.relative_to(ROOT))))
    write_csv(PUBLIC/'累计1500配置成绩.csv',cumulative)
    write_csv(PUBLIC/'开发对照.csv',devrows);write_csv(PUBLIC/'全部候选含负例.csv',attempts)
    write_csv(PUBLIC/'转移对照.csv',transfer['rows']);atomic_json(PUBLIC/'开发诊断与轨迹证据.json',dev)
    atomic_json(PUBLIC/'开发执行manifest.json',read_json(args.dev/'manifest.json'))
    # Exact input/config/official consistency is audited across every attempt.
    source_groups=defaultdict(set)
    for row in attempts:
        rec=read_json(row['record_path']); h=rec['hashes']
        for field in ('config_sha256','official_py_sha256'):
            source_groups[field].add(json.dumps(h[field],sort_keys=True))
        source_groups['graph_'+row['case']].add(h['graph_sha256'])
        if rec['status']=='success':
            if hashlib.sha256(Path(rec['result_path']).read_bytes()).hexdigest()!=rec['result_sha256']:
                raise AssertionError('official artifact hash mismatch')
    if any(len(v)!=1 for v in source_groups.values()):raise AssertionError('inconsistent evaluation inputs')
    atomic_json(PUBLIC/'输入一致性核验.json',{key:list(values) for key,values in source_groups.items()})
    paired=[]
    for case,n in sorted({(x['case'],x['cores']) for x in transfer['rows']}):
        methods={x['method']:x for x in transfer['rows'] if (x['case'],x['cores'])==(case,n)}
        original,old,new=[methods[x]['makespan'] for x in ('original','old','new')]
        baseline=float(lookup[case,1,n]['original_singlecore'])
        paired.append(dict(case=case,cores=n,original=original,old=old,new=new,
                           gain_vs_original=1-new/original,gain_vs_old=1-new/old,
                           delta_speedup=baseline/new-baseline/original,
                           errors=sum(x['errors'] for x in methods.values())))
    gains={n:sum(x['gain_vs_original'] for x in paired if x['cores']==n)/sum(x['cores']==n for x in paired) for n in (3,5)}
    gate=dict(winning_graphs=len({x['case'] for x in paired if x['new']<x['original']}),
              mean_delta_speedup=sum(x['delta_speedup'] for x in paired)/len(paired),
              mean_gain_by_core=gains,worst_regression=max(0,max(-x['gain_vs_original'] for x in paired)),
              errors=sum(x['errors'] for x in paired),prefix_identical=all(read_json(p)['identical'] for p in args.transfer.glob('case_*/n*/prefix_check.json')))
    gate['passed']=gate['winning_graphs']>=2 and gate['mean_delta_speedup']>0 and min(gains.values())>=0 and gate['worst_regression']<=.005 and gate['errors']==0 and gate['prefix_identical']
    gate['quality_conditions_passed']=gate['winning_graphs']>=2 and gate['mean_delta_speedup']>0 and min(gains.values())>=0 and gate['worst_regression']<=.005
    gate['time_exhausted_before_new_generation']=time_starved
    write_csv(PUBLIC/'转移配对结果.csv',paired)
    means={p:sum(float(x['speedup']) for x in cumulative if int(x['problem'])==p and int(x['cores'])==5)/100 for p in (1,2,3)}
    result=dict(development_calls=dev['total_calls'],transfer_calls=transfer['calls'],verification_calls=len(replayed),
                total_fresh_calls=sum(not x['cache_hit'] for x in attempts)+sum(not x['cache_hit'] for x in replayed.values()),
                transfer_gate=gate,cumulative_means_5core=means,selected=portable,
                cumulative_scope='Best-ever plan library; not uniform from-scratch B12 performance.')
    atomic_json(PUBLIC/'结论.json',result)
    lines=['# P1瓶颈重划：实现与实证结果','',
      '本轮实现核心候选生成，完成开发与预选同类转移验证。旧默认、P2/P3和官方评测均未改。',
      '', '## 开发：两个实质突破，两个未解决负例','',
      '|图/核数|当前强解|本轮保留最优|降时|官方调用|','|---|---:|---:|---:|---:|']
    for x in devrows:lines.append(f"|{x['case']}/{x['cores']}|{x['before']}|{x['after']}|{x['reduction']:.2%}|{x['calls']}|")
    lines += ['', '051、075原最大Task内计算，官方轨迹中的同时活跃核心数均从1增至5，最后计算完成明显提前；证据详见《开发诊断与轨迹证据.json》。043本轮只改善尾部，原最重Task未变，不与此前266766的旧起点混算。',
      '', '消融结果：051分支重划320414，宽度4分配仍320414，合并后293518；075对应738999、734127、720755。主要收益来自划分释放并行，束搜索收益较小，合并有追加收益。',
      '', '代价也真实存在：075额外COPY从5641288增至20569380字节，Task从1148增至4323；完工仍明显提前。047分支候选把Task从20增至6368、COPY从1331486增至7388022，变慢；064同样失败，通信路线虽然减少COPY，却仍未超过旧解。不能仅以Task少、COPY少或负载均衡判定成功。',
      '', '## 从头统一B12转移验证','',
      '|图/核数|原portfolio|相同前缀+旧闭环|相同前缀+新重划|新对原降时|','|---|---:|---:|---:|---:|']
    for x in paired:lines.append(f"|{x['case']}/{x['cores']}|{x['original']}|{x['old']}|{x['new']}|{x['gain_vs_original']:.2%}|")
    lines += ['', '获胜候选的归因：048的3/5核均由分支重划获胜；056的3核由通信重划247875再合并至244205；068的3核由通信重划298615再合并至290734。两类候选均有独立同类图支持，且后两例的合并追加收益可直接核验。']
    lines += ['',f"转移门槛：{'通过' if gate['passed'] else '未通过'}。单看质量条件：{'通过' if gate['quality_conditions_passed'] else '未通过'}；共{gate['winning_graphs']}张不同图获胜，3/5核平均降时分别为{gains[3]:.2%}/{gains[5]:.2%}，最差退步{gate['worst_regression']:.2%}，失败或超时{gate['errors']}次。完整值见《结论.json》。每次从头最多12次调用、180秒，构造和所有输入候选求解均计时。没有免费使用历史精选解。",'',
      f"新路有{len(time_starved)}格在起始阶段就耗尽时间，未生成重划候选，详见结论中的time_exhausted_before_new_generation。预留官方调用次数并不等于预留求解时间；这需要另行冻结新时间分配规则验证，不能给本轮临时加时后改判。",'',
      f"开发{dev['total_calls']}次，转移{transfer['calls']}次，独立复评{len(replayed)}次；实际新官方调用合计{result['total_fresh_calls']}次。复评只确认固定选择，不参与B12选优。全部负例与哈希保留；大体积官方原始轨迹位于本地my_runs，精选方案、复评记录与摘要已入Git。",'',
      '## 当前累计成绩与下一步','',
      f"五核累计平均加速比：P1 **{means[1]:.5f}**，P2 **{means[2]:.5f}**，P3 **{means[3]:.5f}**。这是累计方案库成绩；本轮未跑1500组合，不能称作新统一版本的全量成绩。原题未提供可据此换算的单一总分公式。",'',
      '当前实现只完成了分支/通信重划、区域外约束、宽度4核心选择、安全合并和统一预算接入。尚未实现分支净收益预测、任意局部交换、多父搜索或精确DDR争用代理。', '']
    if not gate['passed']:
        lines += ['**按预登记规则停止晋级：不改默认，不跑冻结推广或1500全量。** 已验证的精选结果可以与队友直接汇合。下一步同时预留调用和时间，使重划有机会执行；然后研究什么时候值得重划：对源头共享输入、分支工作量、同步层数和边界搬运联合估计净收益，为047/064这样的负例减少无效切分。开发观察中，047/064分支候选约95%的Task总计算量不超过100周期；051/075约一半。这只是下一轮可检验特征，不能直接硬编码为必然有效的阈值。之后在新开发规则冻结后另留验证图，不能在这8格反复调参再宣称泛化。', '',
          '队友现有强解和稳定评测仍是可靠起点；本轮证明核心划分确有突破口，也说明把候选加入portfolio并不自动产生统一算法提升。后续应保留强基线的预算机会，先修候选选择和求解耗时，再决定如何替换原候选。']
    else:
        lines += ['已通过同类转移门槛；下一步按原先冻结的8图、2—5核推广面板验证，不重新挑图。']
    (PUBLIC/'审阅与推进结果.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
