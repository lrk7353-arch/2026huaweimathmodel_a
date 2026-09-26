"""Read existing official evidence; never run an evaluator or change a plan.

--collect-local copies compact events from retained local official results.
Without that flag, every input is tracked and analysis is portable.
"""
import argparse
from collections import Counter, OrderedDict
import csv
import gzip
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPRINT = HERE.parent
DIRECT = SPRINT.parent
REPO = DIRECT.parents[1]
COLD = DIRECT / '真机启发攻坚_20260926/从头完整1500'
MICRO = REPO / 'A题研究/方案审阅/cache_microtests'
ARCHIVE = HERE / '五核官方缓存事件.json.gz'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write_json(name, data):
    (HERE / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def write_csv(name, rows):
    with (HERE / name).open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def collect(records):
    compact = []
    for item in records:
        if item['problem'] != 3 or item['cores'] != 5:
            continue
        rec = item['best_record']
        path = Path(rec['result_path'])
        assert digest(path) == rec['result_sha256'], item['case']
        with gzip.open(path, 'rt') as f:
            result = json.load(f)
        assert result['makespan'] == rec['metrics']['makespan']
        assert result['cache_stats'] == rec['metrics']['cache_stats']
        ops = [dict(op, core_id=core['core_id']) for core in result['per_core_timeline']
               for op in core['ops'] if op['op'] in ('COPY_IN', 'COPY_OUT')]
        compact.append(dict(case=item['case'], source_result_sha256=rec['result_sha256'],
                            plan_sha256=rec['hashes']['plan_sha256'],
                            makespan=result['makespan'], cache_stats=result['cache_stats'],
                            capacity=result['cache_capacity_bytes'],
                            cache_events=result['cache_events'], copy_timeline=ops,
                            cache_final_entries=result['cache_final_entries'],
                            cache_used_bytes_final=result['cache_used_bytes_final']))
    assert len(compact) == 100
    compact.sort(key=lambda x: x['case'])
    with ARCHIVE.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', mtime=0, filename='') as z:
        z.write(json.dumps(compact, ensure_ascii=False, separators=(',', ':')).encode())


def pool_intervals(ops):
    events = []
    for op in ops:
        pool = op.get('memory_path')
        if pool in ('DDR', 'CACHE_READ'):
            events += [(op['start'], pool, 1), (op['end'], pool, -1)]
    active = Counter()
    busy = Counter()
    maximum = Counter()
    overlap = 0
    prev = 0
    for time, pool, delta in sorted(events):
        dt = time - prev
        for p in ('DDR', 'CACHE_READ'):
            if active[p]:
                busy[p] += dt
        if active['DDR'] and active['CACHE_READ']:
            overlap += dt
        active[pool] += delta
        assert active[pool] >= 0
        maximum[pool] = max(maximum[pool], active[pool])
        prev = time
    assert not any(active.values())
    return busy, maximum, overlap


def classify(item):
    entries, inserted, missed = OrderedDict(), set(), set()
    counts, sizes = Counter(), Counter()
    peak = used = evictions = 0
    last_time = -1
    examples = {}
    for index, event in enumerate(item['cache_events']):
        time, tid, size = event['time'], event['tensor_id'], event['size_bytes']
        assert time >= last_time
        last_time = time
        kind = event['event']
        if kind == 'insert':
            assert tid not in entries and size <= item['capacity']
            removed = []
            while entries and used + size > item['capacity']:
                key, old = entries.popitem(last=False)
                used -= old
                removed.append(key)
            assert removed == event['evicted_tensor_ids']
            evictions += len(removed)
            entries[tid] = size
            inserted.add(tid)
            used += size
            assert used == event['used_bytes'] and used <= item['capacity']
            peak = max(peak, used)
            continue
        assert kind in ('hit', 'miss') and size > 0
        if kind == 'hit':
            assert tid in entries
            category = 'hit'
        else:
            assert tid not in entries
            if size > item['capacity']:
                category = 'oversize_miss'
            elif tid in inserted:
                category = 'post_eviction_miss'
            elif tid in missed:
                category = 'pre_first_insert_repeat_miss'
            else:
                category = 'first_miss'
            missed.add(tid)
        counts[category] += 1
        sizes[category] += size
        if category not in examples:
            examples[category] = dict(event_index=index, event=event)
    stats = item['cache_stats']
    assert sizes['hit'] == stats['hit_bytes']
    assert sum(sizes.values()) - sizes['hit'] == stats['miss_bytes']
    total_bytes = stats['hit_bytes'] + stats['miss_bytes']
    assert abs(stats['hit_rate'] - (stats['hit_bytes'] / total_bytes if total_bytes else 0)) < 1e-12
    assert counts['hit'] == stats['copy_in_hits']
    assert sum(counts.values()) - counts['hit'] == stats['copy_in_misses']
    assert list(entries.items()) == [(x['tensor_id'], x['size_bytes']) for x in item['cache_final_entries']]
    assert used == item['cache_used_bytes_final']
    busy, maximum, overlap = pool_intervals(item['copy_timeline'])
    assert max(busy.values(), default=0) <= item['makespan']
    row = dict(case=item['case'], makespan=item['makespan'], plan_sha256=item['plan_sha256'],
               hit_rate=stats['hit_rate'], peak_cache_bytes=peak, evicted_entries=evictions)
    for c in ('hit', 'first_miss', 'pre_first_insert_repeat_miss', 'post_eviction_miss', 'oversize_miss'):
        row[c + '_count'] = counts[c]
        row[c + '_bytes'] = sizes[c]
    row.update(ddr_busy_cycles=busy['DDR'], cache_busy_cycles=busy['CACHE_READ'],
               simultaneous_pools_cycles=overlap, max_ddr_concurrency=maximum['DDR'],
               max_cache_concurrency=maximum['CACHE_READ'])
    return row, examples


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--collect-local', action='store_true')
    args = parser.parse_args()
    records = read(COLD / 'selected_records.json')
    lookup = {(r['case'], r['problem'], r['cores']): r['best_record'] for r in records}
    assert set(lookup) == {(f'case_{i:03d}', p, n) for i in range(1, 101)
                           for p in (1, 2, 3) for n in range(1, 6)}
    config_hash = digest(REPO / '选题分析/A题附件/data/config.txt')
    source_hashes = {p.name: digest(p) for p in (REPO / '选题分析/A题附件/code').glob('*.py')}
    for record in lookup.values():
        assert record['hashes']['config_sha256'] == config_hash
        for name, expected in record['hashes']['official_py_sha256'].items():
            assert source_hashes[name] == expected, name
    if args.collect_local:
        collect(records)
    with gzip.open(ARCHIVE, 'rt') as f:
        compact = json.load(f)
    assert len(compact) == 100
    assert {item['case'] for item in compact} == {f'case_{i:03d}' for i in range(1, 101)}
    rows, witnesses = [], {}
    for item in compact:
        original = lookup[item['case'], 3, 5]
        assert item['source_result_sha256'] == original['result_sha256']
        assert item['plan_sha256'] == original['hashes']['plan_sha256']
        assert item['cache_stats'] == original['metrics']['cache_stats']
        assert item['makespan'] == original['metrics']['makespan']
        assert item['capacity'] == 1048576
        row, examples = classify(item)
        rows.append(row)
        witnesses[item['case']] = examples
    assert len(rows) == 100
    write_csv('五核容量与时序分类.csv', rows)
    pairs, curve = [], []
    for n in range(1, 6):
        for i in range(1, 101):
            case = f'case_{i:03d}'
            a, b = lookup[case, 2, n], lookup[case, 3, n]
            ma, mb = a['metrics'], b['metrics']
            cache = mb['cache_stats']
            pairs.append(dict(case=case, cores=n, p2_time=ma['makespan'], p3_time=mb['makespan'],
                              ratio_p2_over_p3=ma['makespan']/mb['makespan'],
                              same_plan=a['hashes']['plan_sha256'] == b['hashes']['plan_sha256'],
                              p2_added_copy=ma['data_movement_bytes']['added_copy_bytes'],
                              p3_added_copy=mb['data_movement_bytes']['added_copy_bytes'],
                              hit_bytes=cache['hit_bytes'], miss_bytes=cache['miss_bytes'], hit_rate=cache['hit_rate']))
        group = [r for r in pairs if r['cores'] == n]
        curve.append(dict(cores=n, count=100, mean_same_core_ratio=sum(r['ratio_p2_over_p3'] for r in group)/100,
                          mean_hit_rate=sum(r['hit_rate'] for r in group)/100,
                          pooled_hit_rate=sum(r['hit_bytes'] for r in group)/sum(r['hit_bytes']+r['miss_bytes'] for r in group),
                          same_plan_count=sum(r['same_plan'] for r in group)))
    write_csv('成熟从头P2P3配对500项.csv', pairs)
    write_csv('核数与缓存汇总.csv', curve)
    micros = []
    for path in sorted(MICRO.glob('*_result.json')):
        result = read(path)
        s = result['cache_stats']
        micros.append(dict(name=path.name.removesuffix('_result.json'), makespan=result['makespan'],
                           hit_bytes=s['hit_bytes'], miss_bytes=s['miss_bytes'], hit_rate=s['hit_rate'],
                           static_added_copy=result['data_movement_bytes']['added_copy_bytes']))
    write_csv('既有微实验正负例.csv', micros)
    sums = {k: sum(r[k] for r in rows) for k in rows[0]
            if (k.endswith('_bytes') or k.endswith('_count')) and k != 'peak_cache_bytes'}
    miss_total = sum(r[k] for r in rows for k in ('first_miss_bytes', 'pre_first_insert_repeat_miss_bytes', 'post_eviction_miss_bytes', 'oversize_miss_bytes'))
    summary = dict(scope='100 graphs, 5 cores, released cold P3 plans; descriptive replay of saved events; no counterfactual simulation',
                   official_calls_added=0, sources=dict(cold_records_sha256=digest(COLD/'selected_records.json'),
                   events_sha256=digest(ARCHIVE), config_sha256=digest(REPO/'选题分析/A题附件/data/config.txt'),
                   official_p3_sha256=digest(REPO/'选题分析/A题附件/code/multicore_cut_evaluate_problem_3.py')),
                   graph_count=100, curves=curve, totals=sums, miss_total_bytes=miss_total,
                   miss_byte_fractions={k:sums[k]/miss_total for k in ('first_miss_bytes','pre_first_insert_repeat_miss_bytes','post_eviction_miss_bytes','oversize_miss_bytes')},
                   graphs_with_eviction=sum(r['evicted_entries']>0 for r in rows),
                   graphs_without_eviction=sum(r['evicted_entries']==0 for r in rows),
                   graphs_with_post_eviction_miss=sum(r['post_eviction_miss_count']>0 for r in rows),
                   graphs_with_pre_insert_repeat=sum(r['pre_first_insert_repeat_miss_count']>0 for r in rows),
                   graphs_with_both_pools_active=sum(r['simultaneous_pools_cycles']>0 for r in rows),
                   graphs_without_hits=sum(r['hit_count']==0 for r in rows),
                   checks='All hit/miss event membership, FIFO order, eviction lists, occupancy, final entries, byte/count sums, timestamps, and provenance hashes match saved official records.')
    write_json('分析摘要.json', summary)
    write_json('事件定位索引.json', witnesses)
    micro_sources = {p.name:digest(p) for p in sorted(MICRO.glob('*.json'))}
    write_json('既有微实验来源哈希.json', micro_sources)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
