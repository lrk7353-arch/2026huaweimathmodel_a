import time
import unittest
from p1_iterative_tasks import search


def plan(i):
    return {'node_to_subgraph': {'0': i}, 'core_schedules': [[i]]}


def record(t, status='success'):
    return {'status': status, 'metrics': {'makespan': t,
            'data_movement_bytes': {'added_copy_bytes': 0}}}


def candidate(i, bound=0):
    return {'name': str(i), 'plan': plan(i), 'metadata': {'lower_bound': bound}}


class IterativeTests(unittest.TestCase):
    def test_single_pass_can_beat_greedy_refresh_by_retaining_parent_tail(self):
        def make(p, r):
            return [candidate(1), candidate(2)] if p == plan(0) else []
        def run(p, _):
            return record({1:99, 2:70}[next(iter(p['node_to_subgraph'].values()))])
        static = search(plan(0), record(100), make, run, 3,
                        time.monotonic()+10, refresh_after_accept=False)
        refreshed = search(plan(0), record(100), make, run, 3,
                           time.monotonic()+10, refresh_after_accept=True)
        self.assertEqual(static[1]['metrics']['makespan'], 70)
        self.assertEqual(refreshed[1]['metrics']['makespan'], 99)
        self.assertEqual(len(static[2]), 2)
        self.assertEqual(static[4], 1)

    def test_regenerates_from_new_parent_and_respects_total_budget(self):
        parents = []
        def make(p, r):
            i = next(iter(p['node_to_subgraph'].values()))
            parents.append((i, r['metrics']['makespan']))
            return [candidate(i+1), candidate(99)]
        def run(p, _):
            i = next(iter(p['node_to_subgraph'].values()))
            return record(100-i)
        p, r, calls, _, generations = search(plan(0), record(100), make, run, 3,
                                               time.monotonic()+10)
        self.assertEqual(parents, [(0,100), (1,99), (2,98)])
        self.assertEqual(r['metrics']['makespan'], 97)
        self.assertEqual(len(calls), 3)
        self.assertEqual(generations, 3)

    def test_duplicate_and_bound_do_not_spend_official_calls(self):
        def make(p, r):
            return [candidate(0), candidate(1, 101), candidate(2, 100)]
        p, r, calls, skips, _ = search(plan(0), record(100), make,
            lambda p, t: record(100), 4, time.monotonic()+10)
        self.assertEqual(len(calls), 1)  # equality must remain eligible
        self.assertEqual(len(skips), 2)
        self.assertEqual(p, plan(0))

    def test_failure_is_charged_once_without_retries(self):
        p, r, calls, _, _ = search(plan(0), record(100),
            lambda p, r: [candidate(1), candidate(1)],
            lambda p, t: record(0, 'timeout'), 3, time.monotonic()+10)
        self.assertEqual(len(calls), 1)
        self.assertEqual(p, plan(0))
        self.assertEqual(r['metrics']['makespan'], 100)


if __name__ == '__main__':
    unittest.main()
