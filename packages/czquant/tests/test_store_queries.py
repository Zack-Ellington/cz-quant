"""Pool reads preserve filtering while excluding unrelated diagnostic history."""
import pytest
from czquant import Store


@pytest.mark.parametrize('pool', [None, 'a'])
@pytest.mark.parametrize('kinds', [None, (), ('transaction',), ('observation',), ('transaction', 'observation')])
@pytest.mark.parametrize('observations', [None, (), ('halt',)])
def test_event_filters_preserve_rows_order_and_cutoffs(tmp_path, pool, kinds, observations):
    store = Store(tmp_path)
    for owner in ['a', 'b']:
        for kind, data in [('transaction', {'reason': 'funding'}), ('observation', {'kind': 'halt'}),
                           ('observation', {'kind': 'scan_complete'})]:
            store.append(kind, data, owner, at='2026-01-01T00:00:00Z')
    all_events = store.events()
    expected = [e for e in all_events if (pool is None or e['portfolio_id'] == pool)
                and (kinds is None or e['type'] in kinds)
                and (observations is None or e['type'] != 'observation' or e['data']['kind'] in observations)
                and e['sequence'] > 1]
    assert store.events(pool, kinds=kinds, observations=observations, after_sequence=1,
                        until='2026-01-01T00:00:00Z') == expected
    assert store.events(pool, kinds=kinds, observations=observations,
                        until='2025-01-01T00:00:00Z') == []


def test_recovery_query_uses_selective_pool_indexes(tmp_path):
    store = Store(tmp_path)
    statements = []
    store.db.set_trace_callback(statements.append)
    store.events('a', kinds=('transaction', 'order', 'observation'), observations=('halt', 'halt_cleared'))
    query = next(s for s in statements if s.startswith('SELECT'))
    plan = store.db.execute('EXPLAIN QUERY PLAN ' + query).fetchall()
    detail = ' '.join(row[3] for row in plan)
    assert 'events_pool_type' in detail and 'events_pool_observation' in detail
