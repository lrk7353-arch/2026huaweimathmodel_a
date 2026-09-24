import importlib.util
from fractions import Fraction
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('build_panels', HERE / 'build_panels.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class SelectionTests(unittest.TestCase):
    def test_tied_midranks_are_exact(self):
        rows = {c: {'features': {f: Fraction(v) for f in p.FEATURES}}
                for c, v in [('z', 0), ('a', 0), ('b', 2)]}
        vectors = p.rank_vectors(rows)
        self.assertEqual(vectors['z'], (1,) * len(p.FEATURES))
        self.assertEqual(vectors['a'], vectors['z'])
        self.assertEqual(vectors['b'], (4,) * len(p.FEATURES))

    def test_medoid_and_tie(self):
        vectors = {'a': (0,), 'b': (1,), 'c': (2,)}
        values, log = p.farthest_cover(['c', 'b', 'a'], 2, vectors)
        self.assertEqual(values, ['b', 'a'])
        self.assertEqual(log[1]['distance_from_prior_set_squared_rank_units'], 1)

    def test_extension_uses_previous_panel_as_anchor(self):
        values, _ = p.farthest_cover(['b', 'c'], 1, {'a': (0,), 'b': (1,), 'c': (3,)}, anchors=['a'])
        self.assertEqual(values, ['c'])

    def test_overlap_refused(self):
        with self.assertRaisesRegex(ValueError, 'overlapping'):
            p.farthest_cover(['a'], 1, {'a': (0,)}, anchors=['a'])

    def test_insufficient_pool_refused(self):
        with self.assertRaises(ValueError):
            p.farthest_cover(['a'], 2, {'a': (0,)})


class ActualStructureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = p.load_features(p.RESEARCH / '方案审阅/结构核验')
        cls.panels = p.select_panels(cls.rows)

    def test_exact_sizes_disjoint_and_thresholds(self):
        x = self.panels
        self.assertEqual([len(x[k]) for k in ('development12', 'extension24', 'stress4')], [12, 24, 4])
        self.assertEqual(len(set(x['development12'] + x['extension24'] + x['stress4'])), 40)
        self.assertTrue(all(self.rows[c]['features']['compute_ops'] <= 4000 for c in x['development12']))
        self.assertTrue(all(self.rows[c]['features']['compute_ops'] <= 10000 for c in x['extension24']))
        self.assertTrue(all(self.rows[c]['features']['compute_ops'] > 10000 for c in x['pressure_population']))

    def test_order_invariance(self):
        self.assertEqual(self.panels, p.select_panels(dict(reversed(list(self.rows.items())))))

    def test_extraneous_performance_fields_have_no_effect(self):
        altered = {c: dict(r, makespan=-9999, wins=9999) for c, r in self.rows.items()}
        self.assertEqual(self.panels, p.select_panels(altered))

    def test_largest_graph_is_stress_anchor(self):
        largest = min(self.rows, key=lambda c: (-self.rows[c]['features']['compute_ops'], c))
        self.assertEqual(self.panels['stress4'][0], largest)


if __name__ == '__main__':
    unittest.main()
