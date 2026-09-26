"""Measure one observation-only optimization without changing production code."""
import gzip
import inspect
import json
from pathlib import Path
import time

from common_run import DATA, GraphIR, write_csv
from p1_latest_audit import OUT
import wait_observation


def main():
    source = inspect.getsource(wait_observation.observe)
    assert source.count("if entry['op_id'] in ir.compute_ids:") == 1
    source = source.replace('events={};compute={};assignment={};',
                            'compute_members=set(ir.compute_ids)\n    events={};compute={};assignment={};')
    source = source.replace("if entry['op_id'] in ir.compute_ids:",
                            "if entry['op_id'] in compute_members:")
    namespace = dict(wait_observation.__dict__)
    exec(compile(source, '<observation-membership-only>', 'exec'), namespace)
    fast = namespace['observe']
    results = []
    for case in ('case_043', 'case_085'):
        parent = json.loads((OUT/'探针运行'/case/'parent.json').read_text())
        ir = GraphIR.from_path(DATA/(case+'.json'))
        plan=json.loads(Path(parent['plan_path']).read_text())
        with gzip.open(parent['result_path'],'rt') as f: raw=json.load(f)
        reference=None
        for repeat in range(2):
            arms = [('tuple',wait_observation.observe),('set',fast)]
            if repeat: arms.reverse()
            for name,fn in arms:
                start=time.perf_counter(); observed=fn(ir,plan,raw,1); elapsed=time.perf_counter()-start
                observed['summary'].pop('compile_seconds')
                if reference is None: reference=observed
                assert observed==reference, 'observation semantics changed'
                results.append(dict(case=case,repeat=repeat,variant=name,seconds=elapsed,outputs_equal=True))
                print(results[-1],flush=True)
                write_csv(OUT/'观察阶段集合索引实测.csv',results)


if __name__=='__main__':
    main()
