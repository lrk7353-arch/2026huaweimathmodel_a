"""Select a structural holdout panel and freeze rules before new evaluations."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import math
import random
import statistics
import subprocess
from common_run import *

ROOT = R.parent


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def tracked_python_hashes():
    paths = subprocess.check_output(['git', 'ls-files', '-z', '*.py'], cwd=ROOT).decode().split('\0')
    return {p: sha(ROOT/p) for p in paths if p}


def select(features):
    """Two representatives per size tertile; structure only, no measured scores."""
    eligible = sorted([x for x in features if x['case'] not in
                       ('case_016', 'case_062', 'case_063', 'case_100')],
                      key=lambda x: (x['compute_ops'], x['case']))
    columns = ('log_ops', 'log_components', 'largest_component_fraction',
               'log_tasks_n5', 'log_median_task_ops_n5')
    chosen = []
    for group_id in range(3):
        group = eligible[group_id*len(eligible)//3:(group_id+1)*len(eligible)//3]
        vectors = {}
        for x in group:
            vectors[x['case']] = tuple((x[k]-min(y[k] for y in group)) /
                (max(y[k] for y in group)-min(y[k] for y in group) or 1) for k in columns)
        centroid = tuple(statistics.mean(v[j] for v in vectors.values()) for j in range(len(columns)))
        dist = lambda a, b: sum((x-y)**2 for x, y in zip(a, b))
        first = min(group, key=lambda x:(dist(vectors[x['case']], centroid), x['case']))
        second = min((x for x in group if x != first),
                     key=lambda x:(-dist(vectors[x['case']], vectors[first['case']]), x['case']))
        chosen.extend([dict(first, size_group=group_id, selection='centroid'),
                       dict(second, size_group=group_id, selection='structural_farthest')])
    return chosen


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    if a.out.exists():
        p.error('protocol file already exists; use a new version')
    features = []
    graphs = {}
    source = R/'直接迭代/第六轮成果'
    scores = {(x['case'], int(x['problem']), int(x['cores'])):x
              for x in csv.DictReader((source/'全部成绩.csv').open(encoding='utf-8-sig'))}
    for i in range(1,101):
        case = f'case_{i:03d}'
        ir = GraphIR.from_path(DATA/(case+'.json'))
        plan = read_json(source/f'方案/p1/n5/{case}_multicore_res.json')
        counts = Counter(plan['node_to_subgraph'].values())
        x = dict(case=case, compute_ops=len(ir.compute_ids), components=len(ir.components),
                 largest_component_fraction=max(len(c.nodes) for c in ir.components)/len(ir.compute_ids),
                 tasks_n5=len(counts), median_task_ops_n5=statistics.median(counts.values()))
        for k, v in [('log_ops',x['compute_ops']),('log_components',x['components']),
                     ('log_tasks_n5',x['tasks_n5']),('log_median_task_ops_n5',x['median_task_ops_n5'])]:
            x[k] = math.log1p(v)
        features.append(x)
        graphs[str((DATA/(case+'.json')).relative_to(ROOT))] = sha(DATA/(case+'.json'))
    chosen = select(features)
    pairs = [(2,3),(2,4),(2,5),(3,4),(3,5),(4,5)]
    random.Random(17).shuffle(pairs)
    jobs = []
    for x, cores in zip(chosen, pairs):
        for n in cores:
            case = x['case']; plan = source/f'方案/p1/n{n}/{case}_multicore_res.json'
            row = scores[case,1,n]
            jobs.append(dict(case=case, cores=n, plan=str(plan.relative_to(ROOT)),
                plan_sha256=sha(plan), original_singlecore=int(row['original_singlecore']),
                expected_baseline=int(row['after']),
                method_order=['single','iterative'] if len(jobs)%2 == 0 else ['iterative','single']))
    assert Counter(x['cores'] for x in jobs) == {2:3,3:3,4:3,5:3}
    fixed = {str(p.relative_to(ROOT)):sha(p) for p in
             [DATA/'config.txt', *sorted((DATA.parent/'code').glob('*.py'))]}
    protocol = dict(protocol_version=1, created_utc=datetime.now(timezone.utc).isoformat(),
        upstream_commit='18e0957475b20e9a3af52ab335dccb2611beefc9',
        implementation_commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        seed=17, call_budget=8, total_seconds=180, evaluation_timeout_seconds=60, workers=2,
        selection_rule='exclude 016/062/063/100; tertiles of compute size; normalized structural centroid and farthest; no performance outcome used',
        selected_features=chosen, all_features=features, jobs=jobs,
        python_sha256=tracked_python_hashes(), official_sha256={**graphs, **fixed},
        promotion=dict(min_distinct_winning_graphs=2, per_core_mean_time_reduction_min=0,
                       mean_reference_speedup_delta_strictly_positive=True, max_regression_pct=.5,
                       allow_evaluation_errors=False, allow_timeouts=False),
        extension_rule='Run the same entire 12-cell panel at budget 12 only if at least two iterative cells exhaust calls and at least one of these accepts a generation>=2 improvement within its final two evaluated candidates; never select only winning cells',
        scope='warm-start structural holdout for this controller; not an unseen dataset or from-scratch algorithm ranking')
    atomic_json(a.out, protocol)
    write_csv(a.out.with_name('结构特征与入选.csv'),
              [dict(x, selected=x['case'] in {y['case'] for y in chosen}) for x in features])
    print(json.dumps({'protocol':str(a.out),'selected':chosen,'jobs':jobs},ensure_ascii=False))


if __name__ == '__main__':
    main()
