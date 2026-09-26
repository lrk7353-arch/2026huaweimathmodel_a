"""Audit recorded proxy ordering within identical paid parents; no evaluations.

Pairs are correlated observations, not an independent estimate of error rate.
Some logged proxies precede the merge transform; this audit intentionally
exposes that limitation rather than treating them as final-plan predictions.
"""
from collections import Counter, defaultdict
import csv
import json
from pathlib import Path


def main():
    here = Path(__file__).resolve().parent
    source = here/'P1接力实验_20260926/run_v1/arms'
    out = here/'P1多尺度联合优化_20260926'
    groups = defaultdict(list)
    for path in sorted(source.glob('*/*/summary.json')):
        state = json.loads(path.read_text())
        for call in state['calls']:
            record = call['record']
            metadata = call.get('metadata', {})
            proxy = metadata.get('proxy', metadata.get('proxy_end'))
            if record['status'] != 'success' or not isinstance(proxy, (int, float)):
                continue
            groups[(state['case'], state['family'], call['parent_record'])].append(
                dict(name=call['name'], proxy=proxy, makespan=record['metrics']['makespan'],
                     record_path=record['record_path']))
    rows, counts, group_counts = [], defaultdict(Counter), Counter()
    for (case, family, parent), candidates in sorted(groups.items()):
        if len(candidates) < 2:
            continue
        group_counts[family] += 1
        for i, a in enumerate(candidates):
            for b in candidates[i+1:]:
                if a['proxy'] == b['proxy']:
                    result = 'proxy_tie_same_time' if a['makespan'] == b['makespan'] else 'proxy_tie_different_time'
                elif a['makespan'] == b['makespan']:
                    result = 'official_tie'
                else:
                    result = 'concordant' if (a['proxy']-b['proxy'])*(a['makespan']-b['makespan'])>0 else 'inverted'
                counts[family][result] += 1
                rows.append(dict(case=case, family=family, parent_record=parent, result=result,
                                 **{'a_'+k:v for k,v in a.items()}, **{'b_'+k:v for k,v in b.items()}))
    summary = dict(scope='Within same graph/family/exact paid parent, successful candidates only; correlated pairs',
                   warning='Logged raw/merge proxies may refer to pre-merge plans; not a calibrated model error rate',
                   group_counts=dict(group_counts), pair_counts={k:dict(v) for k,v in counts.items()},
                   total_pairs=len(rows), new_official_calls=0)
    out.mkdir(parents=True, exist_ok=True)
    (out/'已有代理排序诊断.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    with (out/'已有代理排序诊断.csv').open('w',encoding='utf-8-sig',newline='') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(summary,ensure_ascii=False))


if __name__ == '__main__':
    main()
