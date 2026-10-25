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


def loss_engine(tmp_path,monkeypatch,complete=False):
    monkeypatch.setenv('STRATEGY_RUNS_DIR',str(tmp_path))
    monkeypatch.setenv('CZQUANT_PORTFOLIO','sum')
    monkeypatch.setattr(paper_ws,'WS_RUNS',str(tmp_path/'diagnostics'))
    store=Store(tmp_path);store.create('sum')
    context={'trade_id':'basket','event':'event','sets':10,'intended_legs':[
        {'ticker':t,'contract':'YES','qty':10} for t in ['A','B','C']]}
    for t in (['A','B','C'] if complete else ['B']):
        lot=store.append('transaction',{'reason':'fill','cash_delta':'-.4','fee':'.1','context':context,
            'legs':[{'market':t,'side':'yes','quantity_delta':'10','price':'.03'}]},'sum')
        store.append('transaction',{'reason':'settlement','cash_delta':'0','fee':'0','context':context,
            'legs':[{'market':t,'side':'yes','quantity_delta':'-10','price':'0','lot_id':lot}]},'sum')
    return paper_ws.Engine(paper_ws.Config()),store


def test_partial_basket_loss_is_recorded_without_halting(tmp_path,monkeypatch):
    engine,store=loss_engine(tmp_path,monkeypatch)
    assert engine.halted is None and engine.cash==999.6 and engine.realized==-.4
    engine._resume()
    records=[e['data'] for e in store.events() if e['data'].get('kind')=='basket_settled']
    assert len(records)==1 and records[0]['fill_status']=='partial' and records[0]['net_pnl']=='-0.4'
    assert paper_ws.Engine(paper_ws.Config()).halted is None


def test_fully_filled_losing_basket_still_halts(tmp_path,monkeypatch):
    engine,store=loss_engine(tmp_path,monkeypatch,complete=True)
    assert engine.halted=='settled trade basket lost $1.20'
    assert paper_ws.Engine(paper_ws.Config()).halted==engine.halted
    assert any(e['data'].get('fill_status')=='complete' for e in store.events())


def test_reviewed_halt_clear_targets_one_record_and_preserves_loss(tmp_path,monkeypatch):
    engine,store=loss_engine(tmp_path,monkeypatch)
    old=engine.broker.observe('halt',{'reason':'settled trade basket lost $0.40'})
    assert paper_ws.Engine(paper_ws.Config()).halted is not None
    engine.broker.observe('halt_cleared',{'halt_id':old,'reason':'Reviewed partial-fill loss; resume paper trial'})
    restarted=paper_ws.Engine(paper_ws.Config())
    assert restarted.halted is None and restarted.realized==-.4
    restarted.broker.observe('halt',{'reason':'kill switch file present'})
    assert paper_ws.Engine(paper_ws.Config()).halted=='kill switch file present'
