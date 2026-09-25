import copy,random,unittest
from common_run import GraphIR,validate_plan
from test_p1_task_refine import graph
from p1_tensor_regions import regions,generate

class Tests(unittest.TestCase):
    def test_large_chains_and_cheap_join_tree(self):
        data=graph(6,[(0,1),(2,3),(1,4),(3,4),(4,5)]).graph
        for op in data['ops']:op['cycles']=500 if op['id']<4 else 13
        for i,t in enumerate(data['tensors']):t['size']=32768 if i<2 else 2
        ir=GraphIR.from_graph(data);blocks,diag=regions(ir)
        self.assertEqual({frozenset(b) for b in blocks},{frozenset([0,1]),frozenset([2,3]),frozenset([4,5])})
        self.assertEqual(diag['scc_groups_eliminated'],0)
    def test_shortcut_needs_convex_closure(self):
        data=graph(3,[(0,1),(1,2),(0,2)]).graph
        for op in data['ops']:op['cycles']=500
        data['tensors'][2]['size']=32768
        blocks,diag=regions(GraphIR.from_graph(data))
        self.assertEqual(blocks,[[0,1,2]])
        self.assertEqual(diag['scc_groups_eliminated'],1)
    def test_random_dags_and_unmodified_input(self):
        rng=random.Random(2509)
        for _ in range(20):
            data=graph(18,[(i,j) for i in range(18) for j in range(i+1,18) if rng.random()<.12]).graph
            for op in data['ops']:op['cycles']=rng.choice([13,50,500])
            for t in data['tensors']:t['size']=rng.choice([2,2048,32768])
            saved=copy.deepcopy(data);ir=GraphIR.from_graph(data)
            for c in generate(ir,3):validate_plan(ir,c['plan'])
            self.assertEqual(data,saved)

if __name__=='__main__':unittest.main()
