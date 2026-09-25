"""Bounded route decisions from this search's official timeline, never history."""
import gzip
import math

from common_run import read_json, score, validate_plan
from analyze_wcc_transition import merged, intersection


def timeline_features(ir, record, cores):
    plan = read_json(record['plan_path'])
    validate_plan(ir, plan)
    core_by_sg = {sg: c for c, schedule in enumerate(plan['core_schedules']) for sg in schedule}
    expected = {int(op): core_by_sg[sg] for op, sg in plan['node_to_subgraph'].items()}
    with gzip.open(record['result_path'], 'rt') as handle:
        import json
        raw = json.load(handle)
    span = score(record)[0]
    # The original P2 result labels scene B but has no problem key; P3 adds it.
    if (raw['makespan'] != span or raw.get('scene') != 'B' or raw.get('problem', 2) != record['problem']
            or raw['num_cores'] != cores or len(plan['core_schedules']) != cores
            or set(expected) != set(ir.compute_ids) or not math.isfinite(span) or span <= 0):
        raise ValueError('Official timeline/plan identity mismatch')
    observed, timings, seen_cores = {}, [], set()
    for core in raw['per_core_timeline']:
        cid = core['core_id']
        if cid in seen_cores or cid not in range(cores):
            raise ValueError('Invalid or duplicate core')
        seen_cores.add(cid)
        intervals = {'PIPE_M': [], 'PIPE_V': []}
        for op in core['ops']:
            start, end = op['start'], op['end']
            if not all(math.isfinite(x) for x in (start, end)) or not 0 <= start <= end <= span:
                raise ValueError('Invalid official interval')
            if op['op_id'] not in expected:
                continue  # Exclude inserted COPY and non-compute operations.
            if op['op_id'] in observed:
                raise ValueError('Duplicate original compute operation')
            observed[op['op_id']] = cid
            if op['pipe'] in intervals:
                intervals[op['pipe']].append((start, end))
        m, v = (merged(intervals[p]) for p in ('PIPE_M', 'PIPE_V'))
        mb, vb = sum(b-a for a, b in m), sum(b-a for a, b in v)
        overlap = intersection(m, v)
        timings.append(dict(core=cid, m_busy=mb, v_busy=vb, mv_overlap=overlap,
                            non_mv_fraction=1-(mb+vb-overlap)/span))
    if observed != expected or seen_cores != set(range(cores)):
        raise ValueError('Timeline does not cover exact compute assignment and cores')
    largest_busy = max(max(t['m_busy'], t['v_busy']) for t in timings)
    return dict(makespan=span, largest_core_pipe_busy=largest_busy,
                fixed_assignment_headroom=1-largest_busy/span, per_core=timings,
                spill_added_copy_bytes=record['metrics']['data_movement_bytes']['spill_added_copy_bytes'],
                memory_peak_by_core=record['metrics'].get('memory_peak_by_core'),
                cache_stats=record['metrics'].get('cache_stats'),
                source_record=record['record_path'],
                scope='Observed original M/V compute only; non-M/V time is not necessarily memory wait; '
                      'headroom is a route heuristic, not a universal lower bound or achievable gain.')


def observed_route(ir, record, cores, structural):
    decision = dict(structural=structural, threshold=0.10, probe=False,
                    basis='Current-run initial official trials only; no case ID or historical score')
    if record is None:
        return dict(decision, route='staged', reason='no_successful_prefix')
    if structural['route'] != 'component_wcc':
        return dict(decision, route='staged', reason='large_component')
    features = timeline_features(ir, record, cores)
    low = features['fixed_assignment_headroom'] < decision['threshold']
    return dict(decision, route='staged' if low else 'component_wcc', probe=low,
                reason='little_fixed_assignment_headroom' if low else 'remaining_schedule_headroom',
                features=features)


def select_wcc_probe(candidates, seen, signature, capacity):
    """One representative late candidate: memory proxy risk, then compute proxy.

    These estimates only order already-generated plans. They cannot certify
    capacity or performance; the selected plan still requires official scoring.
    """
    ranked = []
    for index, candidate in enumerate(candidates):
        if signature(candidate['plan']) in seen:
            continue
        meta = candidate.get('metadata', {})
        proxy = meta.get('per_core_proxy', [])
        if meta.get('strategy') != 'window_interleave' or not proxy:
            continue
        ends = [p.get('compute_only_predicted_end') for p in proxy]
        if any(not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in ends):
            continue
        risk = max((values.get(kind, 0) / max(1, capacity.get(kind, 1))
                    for p in proxy for values in [p.get('compute_touch_live_bytes_proxy', {})]
                    for kind in ('L1', 'UB')), default=0)
        # Below capacity, do not trade a better compute proxy for less memory.
        rank = (max(0, risk-1), max(ends), index)
        ranked.append((rank, candidate, dict(memory_proxy_ratio=risk, compute_proxy=max(ends))))
    if not ranked:
        return None, None
    _, candidate, details = min(ranked, key=lambda item: item[0])
    return candidate, dict(name=candidate['name'], **details,
                          scope='one charged probe; proxy ranking is not official performance')
