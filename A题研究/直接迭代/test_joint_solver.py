import unittest
from joint_solver import RetainedQueues,exact,run
class QueueTests(unittest.TestCase):
    def test_old_parent_gets_two_charged_trials_before_new_parent(self):
        q=RetainedQueues(2)
        q.add("legacy","old",["a","b","deep"])
        self.assertEqual(q.pop("legacy"),("a","old"))
        q.charged("legacy","old")
        q.add("legacy","new",["n1","n2"])
        self.assertEqual(q.pop("legacy"),("b","old"))
        q.charged("legacy","old")
        self.assertEqual(q.pop("legacy"),("n1","new"))
        q.charged("legacy","new")
        self.assertEqual(q.pop("legacy"),("n2","new"))
        q.charged("legacy","new")
        self.assertEqual(q.pop("legacy"),("deep","old"))
    def test_exhausted_queue_releases_next_parent(self):
        q=RetainedQueues()
        q.add("data","empty",[])
        q.add("data","other",[1])
        self.assertEqual(q.pop("data"),(1,"other"))
        q.charged("data","other")
        self.assertEqual(q.pop("data"),(None,None))
    def test_mapping_order_is_not_canonicalized(self):
        self.assertNotEqual(exact({"node_to_subgraph":{"1":1,"2":2}}),
                            exact({"node_to_subgraph":{"2":2,"1":1}}))
    def test_bad_budgets_rejected_before_filesystem_mutation(self):
        for budget in (0,-1,True,1.5):
            with self.assertRaises(ValueError):
                run("case_009",2,5,"unused",budget=budget)
if __name__=="__main__":unittest.main()
