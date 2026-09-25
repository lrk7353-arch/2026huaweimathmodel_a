"""Mature trace/WCC refinements inside the same unified evaluation budget."""
import gzip
import json
import time
from common_run import read_json


def observed_candidates(structure, scene, cores, record, generation, deadline):
    if scene not in (2, 3) or time.monotonic() >= deadline:
        return
    ir, plan = structure.ir, read_json(record['plan_path'])
    parent_hash = record['hashes']['plan_sha256']
    if len(ir.components) >= cores and structure.profile['minor_pipe_fraction'] >= .1:
        from controller import generate_wcc_candidates
        pool, _ = generate_wcc_candidates(ir, plan, num_cores=cores, max_candidates=3, seed=17)
        for candidate in pool:
            if time.monotonic() >= deadline:
                return
            candidate['name'] = f'observed_wcc_g{generation}_' + candidate['name']
            candidate['metadata'] = {**candidate['metadata'], 'new_strategy': False,
                'family': 'observed_mature_wcc', 'parent_plan_sha256': parent_hash}
            yield candidate
    if time.monotonic() >= deadline:
        return
    from advanced_solver.trace_refine import generate_trace_candidates
    with gzip.open(record['result_path'], 'rt', encoding='utf-8') as stream:
        raw = json.load(stream)
    pool, _ = generate_trace_candidates(ir, plan, raw, num_cores=cores,
        max_candidates=4, round_index=generation, seed=17)
    for candidate in pool:
        if time.monotonic() >= deadline:
            return
        candidate['name'] = f'observed_trace_g{generation}_' + candidate['name']
        candidate['metadata'] = {**candidate['metadata'], 'new_strategy': False,
            'family': 'observed_mature_trace', 'parent_plan_sha256': parent_hash}
        yield candidate
