"""Bounded, persistent structural lineages; independent of graph identity.

The answer archive belongs to the caller. This search archive deliberately
does not apply a near-best score gate before a complete repair has been tried.
"""
from dataclasses import dataclass, field
import hashlib
import json


def exact_signature(plan):
    return hashlib.sha256(json.dumps(plan, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def structural_signature(plan, problem):
    """Ignore Task/core labels and local order, never use this for eval dedup."""
    owner = {t: c for c, tasks in enumerate(plan['core_schedules']) for t in tasks}
    operations = sorted((int(o), t) for o, t in plan['node_to_subgraph'].items())
    core_names, task_names = {}, {}
    features = []
    for op, task in operations:
        core = core_names.setdefault(owner[task], len(core_names))
        if problem == 1:
            block = task_names.setdefault(task, len(task_names))
            features.append((op, block, core))
        else:
            features.append((op, core))
    return hashlib.sha256(repr(features).encode()).hexdigest()


def record_score(record):
    m = record['metrics']
    return m['makespan'], m['data_movement_bytes']['added_copy_bytes']


@dataclass
class Lineage:
    number: int
    record: dict
    shape: str
    origin: str
    protected_remaining: int = 2
    visits: int = 0
    depth: int = 0
    stagnant: int = 0
    last_turn: int = -1
    exhausted: bool = False
    frames: list = field(default_factory=list)
    alternate_seeds: list = field(default_factory=list)


class PersistentBudget:
    """At most width active lineages with a finite, explicitly charged lease.

    Seeds not yet admitted stay in a bounded source list, not an unbounded
    search beam. A local improvement inherits visits and the remaining lease.
    """
    def __init__(self, width=3, repair_quota=2):
        if width < 1 or repair_quota < 1:
            raise ValueError('positive width and repair quota required')
        self.width, self.repair_quota = width, repair_quota
        self.active, self.waiting, self.retired, self.events = [], [], [], []
        self.next_id = 0
        self.turn = 0

    def add_seed(self, record, shape, origin):
        if record.get('status') != 'success':
            return
        # A different order on an existing ownership/partition is a frame of
        # the same branch. Keep the best seed as its entry, preserving origin.
        for item in self.waiting:
            if item['shape'] == shape:
                previous = dict(record=item['record'], origin=item['origin'])
                if record_score(record) < record_score(item['record']):
                    item.update(record=record, origin=origin)
                    item['alternates'].append(previous)
                else:
                    item['alternates'].append(dict(record=record, origin=origin))
                # Two alternate orders within the same structure, not three
                # extra structural branches. Both records are already paid.
                item['alternates'] = item['alternates'][:2]
                return
        self.waiting.append(dict(record=record, shape=shape, origin=origin, alternates=[]))

    def start(self):
        # Best official seed plus structurally distinct earlier controls. This
        # leaves slow w100/component parents eligible without knowing case IDs.
        if self.waiting:
            best = min(self.waiting, key=lambda x: record_score(x['record']))
            self.waiting.remove(best)
            self.waiting.insert(0, best)
        self._fill()

    def _fill(self):
        while self.waiting and len(self.active) < self.width:
            item = self.waiting.pop(0)
            if item['shape'] in {b.shape for b in self.active}:
                self.events.append(dict(event='seed_shape_already_active', origin=item['origin']))
                continue
            branch = Lineage(self.next_id, item['record'], item['shape'], item['origin'],
                             protected_remaining=self.repair_quota,
                             alternate_seeds=item['alternates'])
            self.next_id += 1
            self.active.append(branch)
            self.events.append(dict(event='admit', lineage=branch.number,
                record=branch.record['record_path'], quota=self.repair_quota,
                origin=branch.origin))

    def choose(self):
        # Retire only exhausted branches or branches that already received
        # their lease and stalled. Never reset visits when their score improves.
        for branch in list(self.active):
            pending_lease = any(getattr(frame, 'stale_lease', 0)>0 for frame in branch.frames)
            retire = branch.exhausted or (self.waiting and branch.protected_remaining == 0
                                         and branch.stagnant >= self.repair_quota and not pending_lease)
            if retire:
                self.active.remove(branch)
                self.retired.append(branch)
                self.events.append(dict(event='retire', lineage=branch.number,
                    reason='exhausted' if branch.exhausted else 'completed_lease_stalled',
                    visits=branch.visits, protected_remaining=branch.protected_remaining))
        self._fill()
        if not self.active:
            return None
        # Every protected lineage gets an actual call before an already-served
        # lineage receives more. Thereafter use deterministic round-robin.
        return min(self.active, key=lambda b: (b.protected_remaining == 0,
            b.visits, b.last_turn, b.number))

    def observe(self, branch, record=None, shape=None):
        branch.visits += 1
        branch.last_turn = self.turn
        self.turn += 1
        branch.protected_remaining = max(0, branch.protected_remaining - 1)
        improved = record is not None and record.get('status') == 'success' and \
            record_score(record) < record_score(branch.record)
        if improved:
            branch.record = record
            branch.shape = shape or branch.shape
            branch.depth += 1
            branch.stagnant = 0
        else:
            branch.stagnant += 1
        return improved

    def summary(self):
        def describe(b):
            return dict(lineage=b.number, origin=b.origin, current_record=b.record['record_path'],
                shape=b.shape, calls=b.visits, depth=b.depth, stagnant=b.stagnant,
                protected_remaining=b.protected_remaining, exhausted=b.exhausted,
                pending_parent_leases=[dict(record=f.record['record_path'], remaining=f.stale_lease)
                    for f in b.frames if f.stale_lease>0])
        return dict(width=self.width, repair_quota=self.repair_quota,
            active=[describe(b) for b in self.active], retired=[describe(b) for b in self.retired],
            waiting=[dict(origin=x['origin'], record=x['record']['record_path']) for x in self.waiting],
            events=self.events, charged_continuation_calls=self.turn)
