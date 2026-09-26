"""Frozen graph-only initialization shared by both continuous-search arms.

Each callable produces one complete candidate. Cached graph-only candidate
pools are private to this run; no historical scores or solutions are accessed.
"""
import time


def factories(ir, problem, cores):
    cache = {}

    def component(index=0):
        if 'components' not in cache:
            from advanced_solver.component_baseline import generate_component_candidates
            cache['components'], _ = generate_component_candidates(ir, cores, max_candidates=24, seed=17)
        return cache['components'][min(index, len(cache['components'])-1)]

    if problem == 1:
        from p1_recipe_bootstrap import specifications, candidate as recipe
        from p1_bounded_bootstrap import candidate as bounded
        spec = specifications(ir)
        # Receive the already researched graph-derived recipes. Task count and
        # compilation cost remain measured, not assumed from the proxy.
        first = spec[-1] if spec[0][0] == 'whole_component_pack' else spec[0]
        return [
            ('graph_recipe', lambda: recipe(ir, cores, first)),
            ('bounded_components', lambda: bounded(ir, cores, 512, 0)),
            ('bounded_dependency_bands', lambda: bounded(ir, cores, 512, max(2, 2*cores))),
            ('whole_component_control', lambda: component(0)),
            ('whole_component_alternative', lambda: component(1)),
            ('second_graph_recipe', lambda: recipe(ir, cores, spec[2])),
        ]

    def operation(name, repair=False):
        if 'operations' not in cache:
            from advanced_solver.operation_assign import generate_operation_candidates
            cache['operations'], _ = generate_operation_candidates(ir, cores, max_candidates=24, seed=17)
        c = next(c for c in cache['operations']
                 if c['name'] == name or name in c.get('metadata', {}).get('aliases', []))
        if not repair:
            return dict(c, name=name)
        from event_frontier import candidates
        paired = next(candidates(ir, problem, cores, c['plan'], time.monotonic()+12))
        paired['name'] = name + '_complete_insertion'
        paired['metadata'] = dict(paired['metadata'], seed_origin=name,
                                  combined_ownership_and_order=True)
        return paired

    def wcc():
        from controller import generate_wcc_candidates
        from p23_observed import select_wcc_probe
        from persistent_budget import exact_signature
        source = component()
        pool, _ = generate_wcc_candidates(ir, source['plan'], num_cores=cores,
                                          max_candidates=24, seed=17, policy='mixed')
        # This ranking does not discard any plan as provably worse. It selects
        # one of the six initialization slots and never loads an official trace.
        chosen, _ = select_wcc_probe(pool, {exact_signature(source['plan'])},
                                    exact_signature, {'L1': 512*1024, 'UB': 128*1024})
        return chosen or component(1)

    return [
        ('whole_component_control', lambda: component(0)),
        ('whole_component_interleave', wcc),
        ('critical_w100_control', lambda: operation('op_critical_path_w100')),
        ('stable_w100_control', lambda: operation('op_stable_id_w100')),
        ('stable_w200_joint', lambda: operation('op_stable_id_w200', True)),
        ('critical_w200_joint', lambda: operation('op_critical_path_w200', True)),
    ]
