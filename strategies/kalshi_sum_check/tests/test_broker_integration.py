import asyncio
import pytest
from czquant import Store
import paper_ws


def test_resume_actual_partial_holdings_and_persistent_halt(tmp_path,monkeypatch):
    monkeypatch.setenv('STRATEGY_RUNS_DIR',str(tmp_path))
    monkeypatch.setenv('CZQUANT_PORTFOLIO','sum')
    monkeypatch.setattr(paper_ws,'WS_RUNS',str(tmp_path/'diagnostics'))
    store=Store(tmp_path);store.create('sum')
    engine=paper_ws.Engine(paper_ws.Config())
    engine.broker.observe('halt',{'reason':'fixture loss'})
    engine._resume()
    assert engine.halted=='fixture loss' and engine.cash==1000
    restarted=paper_ws.Engine(paper_ws.Config())
    assert restarted.halted=='fixture loss' and restarted.cash==1000
    assert len([e for e in store.events() if e['type']=='transaction'])==1


def test_timed_run_propagates_task_failure(tmp_path,monkeypatch):
    monkeypatch.setenv('STRATEGY_RUNS_DIR',str(tmp_path));monkeypatch.setenv('CZQUANT_PORTFOLIO','sum')
    monkeypatch.setattr(paper_ws,'WS_RUNS',str(tmp_path/'diagnostics'))
    Store(tmp_path).create('sum')
    class Feed:
        mode='rest_poll'
        def __init__(self,*args): pass
        async def run(self): raise RuntimeError('fixture disconnect')
    monkeypatch.setattr(paper_ws,'RestPollFeed',Feed)
    engine=paper_ws.Engine(paper_ws.Config(feed='rest',duration_minutes=.01))
    async def idle(): await asyncio.sleep(10)
    for name in ('discover_loop','allocate_loop','settle_loop','heartbeat_loop'):
        monkeypatch.setattr(engine,name,idle)
    with pytest.raises(RuntimeError,match='fixture disconnect'): asyncio.run(engine.run())
