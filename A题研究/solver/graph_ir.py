"""Immutable compute-component view of the original A graph (stdlib only)."""
from collections import defaultdict, deque
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Dict, FrozenSet, List, Mapping, Tuple

COPY_TYPES = frozenset(("COPY_IN", "COPY_OUT"))
PIPES = frozenset(("PIPE_M", "PIPE_V", "PIPE_MTE2", "PIPE_MTE3"))


def _integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError("{} must be a nonnegative integer".format(name))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: {!r}".format(key))
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("nonstandard JSON number: {}".format(value))


class _DSU:
    def __init__(self, nodes):
        self.parent = {n: n for n in nodes}
        self.size = {n: 1 for n in nodes}

    def find(self, n):
        while self.parent[n] != n:
            self.parent[n] = self.parent[self.parent[n]]
            n = self.parent[n]
        return n

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a != b:
            if self.size[a] < self.size[b]:
                a, b = b, a
            self.parent[b] = a
            self.size[a] += self.size[b]


@dataclass(frozen=True)
class Component:
    id: int
    nodes: Tuple[int, ...]
    work_m: int
    work_v: int
    work_other: int
    input_ids: FrozenSet[int]
    input_bytes: int

    @property
    def compute_work(self):
        return self.work_m + self.work_v + self.work_other


@dataclass
class GraphIR:
    graph: dict
    path: Path
    ops: Dict[int, dict]
    tensors: Dict[int, dict]
    compute_ids: Tuple[int, ...]
    successors: Dict[int, Tuple[int, ...]]
    predecessors: Dict[int, Tuple[int, ...]]
    components: Tuple[Component, ...]
    component_by_op: Dict[int, int]
    input_sizes: Dict[int, int]
    input_components: Dict[int, Tuple[int, ...]]
    total_work_m: int
    total_work_v: int

    @classmethod
    def from_path(cls, path):
        path = Path(path).resolve()
        with path.open(encoding="utf-8") as handle:
            graph = json.load(handle, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        return cls.from_graph(graph, path)

    @classmethod
    def from_graph(cls, graph, path="<memory>"):
        """Build a view without changing any input dict, IDs, labels or edges.

        Dependence follows tensor producers to consumers and contracts COPY
        chains. Co-consumers of one root input are deliberately NOT joined.
        """
        if not isinstance(graph, dict):
            raise ValueError("graph must be an object")
        for key in ("ops", "tensors", "edges"):
            if not isinstance(graph.get(key), list):
                raise ValueError("graph.{} must be a list".format(key))
        ops, tensors, all_ids = {}, {}, set()
        for kind, target in (("ops", ops), ("tensors", tensors)):
            for node in graph[kind]:
                if not isinstance(node, dict):
                    raise ValueError("graph node must be an object")
                ident = node.get("id")
                _integer(ident, "node id")
                if ident in all_ids:
                    raise ValueError("duplicate node id: {}".format(ident))
                all_ids.add(ident)
                target[ident] = node
                if kind == "ops":
                    _integer(node.get("cycles"), "cycles")
                    if node.get("pipe") not in PIPES or not isinstance(node.get("op"), str):
                        raise ValueError("invalid operator pipe/type")
                else:
                    _integer(node.get("size"), "tensor size")
                    if node.get("pos") not in ("DDR", "L1", "UB"):
                        raise ValueError("invalid tensor position")
        producers, consumers = defaultdict(set), defaultdict(set)
        full_succ = {i: set() for i in ops}
        seen = set()
        for edge in graph["edges"]:
            if not isinstance(edge, dict):
                raise ValueError("edge must be an object")
            a, b = edge.get("source"), edge.get("target")
            _integer(a, "edge source")
            _integer(b, "edge target")
            if a not in all_ids or b not in all_ids:
                raise ValueError("unknown edge endpoint")
            if (a, b) in seen:
                raise ValueError("duplicate edge")
            seen.add((a, b))
            if a in ops and b in tensors:
                producers[b].add(a)
            elif a in tensors and b in ops:
                consumers[a].add(b)
            elif a in ops and b in ops:
                # Input schema requires an Op--Tensor bipartite graph. Internal
                # evaluator MEMORY_REUSE edges are not original input edges.
                raise ValueError("original input op-to-op edge is forbidden by the task")
            else:
                raise ValueError("tensor-to-tensor edge is unsupported")
        for tid in tensors:
            for p in producers[tid]:
                full_succ[p].update(consumers[tid])
        indegree = {i: 0 for i in ops}
        for next_ops in full_succ.values():
            for j in next_ops:
                indegree[j] += 1
        queue = deque(sorted(i for i in ops if indegree[i] == 0))
        topo = []
        while queue:
            i = queue.popleft()
            topo.append(i)
            for j in sorted(full_succ[i]):
                indegree[j] -= 1
                if indegree[j] == 0:
                    queue.append(j)
        if len(topo) != len(ops):
            raise ValueError("input graph has a dependency cycle")
        eligible = {i for i, op in ops.items() if op["op"] not in COPY_TYPES}
        copy_reach = {}
        for i in reversed(topo):
            if i not in eligible:
                reached = set()
                for j in full_succ[i]:
                    reached.update((j,) if j in eligible else copy_reach[j])
                copy_reach[i] = reached
        succ = {}
        for i in sorted(eligible):
            reached = set()
            for j in full_succ[i]:
                reached.update((j,) if j in eligible else copy_reach[j])
            succ[i] = tuple(sorted(reached))
        pred = {i: [] for i in eligible}
        dsu = _DSU(eligible)
        for i, next_ops in succ.items():
            for j in next_ops:
                pred[j].append(i)
                dsu.union(i, j)
        members = defaultdict(list)
        for i in sorted(eligible):
            members[dsu.find(i)].append(i)
        groups = sorted(members.values(), key=lambda g: (-len(g), g[0]))
        by_op = {op: cid for cid, group in enumerate(groups) for op in group}
        component_inputs = [set() for _ in groups]
        input_sizes, input_components = {}, {}
        for tid, tensor in sorted(tensors.items()):
            if tensor["pos"] != "DDR" or producers[tid]:
                continue
            readers = [i for i in consumers[tid] if ops[i]["op"] == "COPY_IN"]
            if not readers:
                continue
            cids = {by_op[i] for reader in readers for i in copy_reach[reader]}
            input_sizes[tid] = tensor["size"]
            input_components[tid] = tuple(sorted(cids))
            for cid in cids:
                component_inputs[cid].add(tid)
        components = []
        for cid, group in enumerate(groups):
            work = defaultdict(int)
            for i in group:
                work[ops[i]["pipe"]] += max(1, ops[i]["cycles"])
            ids = frozenset(component_inputs[cid])
            components.append(Component(cid, tuple(group), work["PIPE_M"], work["PIPE_V"],
                                        work["PIPE_MTE2"] + work["PIPE_MTE3"], ids,
                                        sum(input_sizes[t] for t in ids)))
        return cls(graph, Path(path), ops, tensors, tuple(sorted(eligible)), succ,
                   {i: tuple(sorted(p)) for i, p in pred.items()}, tuple(components), by_op,
                   input_sizes, input_components, sum(c.work_m for c in components),
                   sum(c.work_v for c in components))
