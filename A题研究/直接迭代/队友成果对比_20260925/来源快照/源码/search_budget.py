"""Small current-run beam and observed gain/cost allocation for cold P2/P3.

This controller never evaluates or loads historical records. Its caller admits
only already charged official successes. Shape dedup affects beam diversity,
not evaluator input dedup, which must retain exact insertion order.
"""
from collections import defaultdict
from common_run import read_json, score
from refine_regions import structure


class SearchBudget:
    def __init__(self, width, local_order, margin=.05):
        if width not in (1,3): raise ValueError('width must be 1 or 3')
        self.width=width; self.margin=margin
        self.families=list(dict.fromkeys(['legacy']+[f for f in local_order if f!='structure']))
        self.beam=[]; self.shapes={}; self.sequence={}; self.visits=defaultdict(int)
        self.blocked=set(); self.structural_exhausted=False; self.continuation=0
        self.stats={f:dict(calls=0,gain=0.,seconds=0.) for f in self.families}

    def add(self, record, best):
        if record.get('status')!='success': return
        for r in (record,best):
            if r['record_path'] not in self.sequence:self.sequence[r['record_path']]=len(self.sequence)
        pool=sorted(self.beam+[record,best], key=lambda r:(score(r),self.sequence[r['record_path']]))
        kept=[]; shapes=set()
        for r in pool:
            if score(r)[0]>(1+self.margin)*score(best)[0]: continue
            path=r['record_path']
            if path not in self.shapes:self.shapes[path]=structure(read_json(r['plan_path']))
            shape=self.shapes[path]
            if shape not in shapes:
                kept.append(r); shapes.add(shape)
            if len(kept)>=self.width: break
        self.beam=kept

    def choose(self, best):
        if not self.structural_exhausted and self.continuation%3==0:
            return 'structure', None
        candidates=[]
        for r in self.beam:
            available=[f for f in self.families if (r['record_path'],f) not in self.blocked]
            if available:
                gap=max(0.,score(r)[0]/max(1,score(best)[0])-1)
                candidates.append((self.visits[r['record_path']]+10*gap,score(r),self.sequence[r['record_path']],r,available))
        if not candidates:
            return (None,None) if self.structural_exhausted else ('structure',None)
        _,_,_,parent,families=min(candidates,key=lambda x:x[:3])
        # One probe per family before reward/cost allocation. The 2% prior keeps
        # a failed first candidate from permanently eliminating a neighborhood.
        untried=[f for f in families if self.stats[f]['calls']==0]
        if untried: family=untried[0]
        else:
            def priority(f):
                s=self.stats[f]
                value=(s['gain']+.02)/(s['calls']+1)
                cost=max(.02,s['seconds']/max(1,s['calls']))
                return value/cost, -self.families.index(f)
            family=max(families,key=priority)
        return family,parent

    def exhaust(self, family, parent):
        if family=='structure': self.structural_exhausted=True
        elif parent is not None:self.blocked.add((parent['record_path'],family))

    def observe(self, family, parent, before, after, seconds):
        if family=='structure_pair':return
        self.continuation+=1
        if family not in self.stats:return
        self.visits[parent['record_path']]+=1
        s=self.stats[family];s['calls']+=1;s['seconds']+=max(0.,seconds)
        # Only a global makespan improvement earns reward, never a weaker
        # parent's local improvement or a secondary COPY-only improvement.
        s['gain']+=max(0.,(before-after)/max(1,before))

    def summary(self):
        return dict(width=self.width,margin=self.margin,retained_records=[r['record_path'] for r in self.beam],
            family_statistics=self.stats,structural_period=3,continuation_calls=self.continuation,
            scope='online heuristic; measured generation+evaluation seconds; decisions may differ across machine loads')
