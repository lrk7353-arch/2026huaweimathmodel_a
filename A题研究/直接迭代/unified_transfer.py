"""Explicit, cost-attributed transfer of partitions across cores and scenes."""
from collections import defaultdict
import hashlib
from pathlib import Path
from common_run import read_json, validate_plan


def transfer_candidate(structure, summary_path, scene, cores, deadline):
    source = read_json(summary_path)
    rec = source.get('best_record')
    if not rec or rec['status'] != 'success':
        raise ValueError('transfer requires a successful source solve summary')
    digest = hashlib.sha256(structure.ir.path.read_bytes()).hexdigest()
    if rec['hashes']['graph_sha256'] != digest:
        raise ValueError('transfer graph mismatch')
    plan_path = Path(rec['plan_path'])
    if hashlib.sha256(plan_path.read_bytes()).hexdigest() != rec['hashes']['plan_sha256']:
        raise ValueError('transfer source plan was changed')
    plan = read_json(plan_path)
    if source['num_cores'] != cores:
        groups = defaultdict(list)
        for o in structure.topo:
            groups[plan['node_to_subgraph'][str(o)]].append(o)
        plan, _ = structure.assign(list(groups.values()), scene, cores,
            ordering='cache_window' if scene == 3 else 'residency', deadline=deadline)
    validate_plan(structure.ir, plan)
    provenance = dict(summary=str(Path(summary_path).resolve()),
        summary_sha256=hashlib.sha256(Path(summary_path).read_bytes()).hexdigest(),
        source_problem=source['problem'], source_cores=source['num_cores'],
        upstream_logical_calls=source['logical_calls'], upstream_new_calls=source['new_calls'],
        upstream_elapsed_seconds=source['elapsed_seconds'],
        accounting='Upstream solve is explicit and not free. Deduplicate shared summary hashes in family totals; target evaluation is separately charged.')
    return dict(name=f"transfer_p{source['problem']}_n{source['num_cores']}", plan=plan,
        metadata=dict(family='explicit_transfer', new_strategy=False, provenance=provenance))
