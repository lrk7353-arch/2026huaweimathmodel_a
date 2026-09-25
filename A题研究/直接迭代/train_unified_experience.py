"""Freeze small, case-independent nearest-neighbor calibration from dev runs."""
import argparse
import hashlib
from pathlib import Path
from common_run import DATA, GraphIR, atomic_json, read_json
from unified_structure import Structure
from unified_search import Ranker, analytical_features


def train(roots, out):
    structures, seen, manifests = {}, set(), {}
    ranker = Ranker()
    for root in roots:
        manifest = root / 'manifest.json'
        manifests[str(root)] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        for path in sorted(root.glob('slots/*/*/*/summary.json')):
            summary = read_json(path)
            case = summary['case']
            if case not in structures:
                structures[case] = Structure(GraphIR.from_path(DATA / (case+'.json')))
            s = structures[case]
            for c in summary['evaluations']:
                rec = c['record']
                key = (summary['graph_sha256'], summary['problem'], rec['hashes']['plan_sha256'])
                if key in seen:
                    continue
                seen.add(key)
                plan = read_json(rec['plan_path'])
                features = analytical_features(s, plan, summary['problem'])
                ranker.observe(c['metadata'].get('family', 'unknown'), features, rec)
    atomic_json(out, dict(experiences=ranker.rows, source_manifests=manifests,
        development_cases=sorted(structures),
        policy='case names are provenance only; predictions consume structural features and candidate family, never identities'))
    print(dict(observations=len(ranker.rows), development_graphs=len(structures)))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runs', type=Path, nargs='+', required=True)
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    train(a.runs, a.out)
