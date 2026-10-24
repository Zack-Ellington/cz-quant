from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from datetime import datetime,timedelta
import pytest
from czquant import Broker, Store
from czquant.paper import Paper
from czquant.monitor import market_pass, account_pass
from czquant.report import compare, generate
from czquant.store import utc

class Venue:
    final = False
    def book(self, ticker):
        return {'orderbook_fp':{'yes_dollars':[['0.39','10']],'no_dollars':[['0.60','10']]}}
    def market(self,ticker):
        return {'ticker':ticker,'series_ticker':'SERIES','status':'finalized' if self.final else 'active',
                **({'settlement_value_dollars':'0.5'} if self.final else {})}
    def get(self,path,params=None):
        return {'series':{'fee_type':'quadratic','fee_multiplier':1},'markets':[{'ticker':'M'}]}

def setup(tmp_path,cash='100'):
    store=Store(tmp_path); store.create('p',cash)
    venue=Venue()
    return store, Broker(store,'p',Paper(venue,latency=0)), venue

def order(side='bid',count='10',price='.40',**extra):
    return {'ticker':'M','side':side,'count':count,'price':price,'time_in_force':'immediate_or_cancel',**extra}

def test_cash_fee_basis_and_nonbinary_settlement(tmp_path):
    store,b,v=setup(tmp_path)
    context={'basket':{'legs':['M','N']},'new_experiment':[1,False,None]}
    b.submit(order(),context)
    state=b.state()
    assert state.cash==D('95.83') and state.cost==D('4.17') and state.fees==D('.17')
    assert next(iter(state.lots.values()))['context']==context
    v.final=True
    market_pass(store,v)
    assert b.state().cash==D('100.83') and b.state().realized==D('.83') and not b.state().lots
    market_pass(store,v)
    assert b.state().cash==D('100.83')

def test_partial_fill_and_consumption_survive_restart(tmp_path):
    store,b,v=setup(tmp_path)
    assert b.submit(order(count='15'))['filled']=='10'
    assert store.reserved('p')==0
    store.close()
    reopened=Store(tmp_path); b=Broker(reopened,'p',Paper(v,0))
    assert b.submit(order())['filled']=='0'
    assert b.state().contributed==100 and b.state().cash==D('95.83')
    reopened.create('other','100')
    assert Broker(reopened,'other',Paper(v,0)).submit(order())['filled']=='10'

def test_close_fifo(tmp_path):
    _,b,_=setup(tmp_path); b.submit(order())
    assert b.submit(order('ask','15','.39'))['filled']=='10'
    assert not b.state().lots
    assert b.state().cash==D('99.56') and b.state().realized==D('-.44')

def test_insufficient_cash_has_no_inventory_effect(tmp_path):
    store,b,_=setup(tmp_path,'1')
    assert b.submit(order())['status']=='rejected'
    assert b.state().cash==1 and not b.state().lots and store.reserved('p')==0

def test_idempotent_submission_and_context(tmp_path):
    _,b,_=setup(tmp_path); request=order(client_order_id='stable')
    b.submit(request,{'x':[{'new':3}]}); b.submit(request,{'x':[{'new':3}]})
    assert len(b.state().lots)==1
    with pytest.raises(ValueError): b.submit(request,{'x':2})

def test_duplicate_fill_conflict_and_overfill(tmp_path):
    store,b,_=setup(tmp_path);b.submit(order(client_order_id='one'))
    r=next(e['data'] for e in store.events() if e['type']=='execution')
    b.fill('one',r['quantity'],r['yes_price'],r['fee'],r['source'],r['evidence'])
    assert b.state().fees==D('.17')
    with pytest.raises(ValueError): b.fill('one','1','.4','0',r['source'],r['evidence'])
    with pytest.raises(ValueError): b.fill('one','1','.4','0','extra',{})

def test_timeout_retains_live_intent_without_resubmission(tmp_path):
    store=Store(tmp_path);store.create('p',mode='live',subaccount=2)
    class Timeout:
        calls=0
        def create_order(self,order):
            self.calls+=1
            raise TimeoutError()
    adapter=Timeout(); b=Broker(store,'p',adapter); request=order(client_order_id='stable')
    with pytest.raises(TimeoutError): b.submit(request,{'group':'x'})
    assert store.reserved('p')>0
    assert b.submit(request,{'group':'x'})['status']=='unknown'
    assert adapter.calls==1 and not b.state().lots
    with pytest.raises(ValueError): b.submit(order(subaccount=3))

def test_failed_paper_read_releases_reservation(tmp_path):
    store,b,v=setup(tmp_path)
    def broken(ticker): raise TimeoutError()
    v.book=broken
    with pytest.raises(TimeoutError): b.submit(order())
    assert store.reserved('p')==0 and b.state().cash==100

def test_concurrent_brokers_cannot_overspend(tmp_path):
    store=Store(tmp_path);store.create('p','5');store.close()
    def submit(_):
        s=Store(tmp_path);b=Broker(s,'p',Paper(Venue(),0));result=b.submit(order());s.close();return result
    with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(submit,range(2)))
    assert sum(D(r.get('filled','0')) for r in results)==10
    assert Broker(Store(tmp_path),'p').state().cash==D('.83')

def test_fok_no_partial_and_unknown_fee_fails(tmp_path):
    store,b,_=setup(tmp_path)
    assert b.submit(order(count='11',time_in_force='fill_or_kill'))['filled']=='0'
    b.adapter.series={'SERIES':{'fee_type':'new_fee'}}
    with pytest.raises(ValueError): b.submit(order())
    assert store.reserved('p')==0 and b.state().cash==100

def test_correction_retains_original(tmp_path):
    store,b,_=setup(tmp_path);b.submit(order());target=next(iter(b.state().lots))
    b.correct([target],'invalid simulated fill')
    assert b.state().cash==100 and any(e['record_id']==target for e in store.events())

def test_report_open_positions_and_cash_reference(tmp_path):
    store,b,_=setup(tmp_path);store.create('cash','100');start=utc();b.submit(order());end=utc()
    metrics,_=compare(store,start,end);m=next(m for m in metrics if m['portfolio']=='p')
    assert m['net_pnl']==D('-.22') and m['fills']==1 and m['fees']==D('.17')
    assert m['realized_pnl']==0 and m['unrealized_change']==D('-.22')
    assert next(m for m in metrics if m['portfolio']=='cash')['net_pnl']==0
    output=tmp_path/'report';result=generate(str(tmp_path),start,end,str(output))
    assert result['metrics']==metrics and (output/'report.html').exists()

def test_stale_mark_not_zero_profit(tmp_path):
    store,b,_=setup(tmp_path);start=utc();b.submit(order())
    end=(datetime.fromisoformat(utc())+timedelta(minutes=5)).isoformat();m=compare(store,start,end)[0][0]
    assert not m['complete'] and m['net_pnl'] is None and m['max_drawdown'] is None

def test_initial_funding_cannot_repeat(tmp_path):
    store,_,_=setup(tmp_path)
    with pytest.raises(ValueError): store.create('p','100')

def test_conflicting_source_is_error(tmp_path):
    store=Store(tmp_path);store.append('observation',{'a':1},source='source')
    with pytest.raises(ValueError): store.append('observation',{'a':2},source='source')

def test_account_evidence_separate_from_paper(tmp_path):
    store,_,_=setup(tmp_path)
    class Account:
        key_id='fixture'
        def get(self,path,params=None): return {'balance':12345} if path=='/portfolio/balance' else {}
        def pages(self,*args,**kwargs): return iter([])
    assert account_pass(store,Account())['status']=='healthy'
    assert any(e['type']=='account_snapshot' for e in store.events())
    assert Broker(store,'p').state().cash==100

def test_collateral_release_is_not_profit(tmp_path):
    store,b,_=setup(tmp_path)
    store.append('transaction',{'reason':'collateral','cash_delta':'10','prepaid_delta':'-10','fee':'0','legs':[]},'p')
    assert b.state().cash==110 and b.state().prepaid==-10 and b.state().realized==0


def test_funding_changes_are_not_returns(tmp_path):
    store,b,_=setup(tmp_path);start=utc()
    store.append('transaction',{'reason':'funding','cash_delta':'100','external_delta':'100','fee':'0','legs':[]},'p')
    metric=compare(store,start,utc())[0][0]
    assert metric['net_pnl']==0 and metric['time_weighted_return']==0 and metric['max_drawdown']==0
    assert metric['return_on_opening_equity'] is None


def test_offline_report_import_has_no_execution_imports():
    import subprocess,sys
    result=subprocess.run([sys.executable,'-c',"import czquant.report,sys; assert 'czquant.broker' not in sys.modules; assert 'czquant.kalshi' not in sys.modules"],capture_output=True)
    assert result.returncode==0,result.stderr

def test_live_mapping_and_transfer_idempotency(tmp_path):
    from czquant.operator import map_live, transfer
    class Account:
        calls=[]
        def pages(self,*args,**kwargs):return iter([])
        def get(self,*args,**kwargs):return {'balance':10000}
        def request(self,method,path,data): self.calls.append(data);return {'transfer_id':'venue'}
    store=Store(tmp_path);api=Account();map_live(store,api,'live',2)
    assert Broker(store,'live').state().cash==100
    with pytest.raises(ValueError):map_live(store,api,'another',2)
    transfer_id='12345678-1234-1234-1234-123456789012'
    transfer(store,api,transfer_id,0,2,100)
    transfer(store,api,transfer_id,0,2,100)
    with pytest.raises(ValueError):transfer(store,api,transfer_id,0,2,200)
    assert api.calls[0]==api.calls[1]


def test_later_correction_restates_prior_effective_window(tmp_path):
    store,b,_=setup(tmp_path);start=utc();b.submit(order());end=utc()
    original=compare(store,start,end,recorded=end)[0][0]['net_pnl']
    b.correct([next(iter(b.state().lots))],'fixture correction')
    assert compare(store,start,end)[0][0]['net_pnl']==0
    assert compare(store,start,end,recorded=end)[0][0]['net_pnl']==original==D('-.22')


def test_multi_price_fill_cash_is_exact(tmp_path):
    _,b,v=setup(tmp_path)
    v.book=lambda ticker: {'orderbook_fp':{'yes_dollars':[['.39','10']], 'no_dollars':[['.60','2'],['.59','1']]}}
    b.submit(order(count='3',price='.41'))
    assert b.state().cash==D('98.73')  # 1.21 premium + .06 fee

def test_live_amend_cancel_keep_context_and_wait_for_reconciliation(tmp_path):
    store=Store(tmp_path);store.create('p',mode='live',subaccount=2)
    class Adapter:
        def create_order(self,request):return {'order':{'order_id':'venue-id'}}
        def amend_order(self,identifier,changes):
            assert identifier=='venue-id'
            return {'order_id':identifier}
        def cancel_order(self,identifier,params):
            assert identifier=='venue-id' and params['subaccount']==2
            return {'order_id':identifier}
    b=Broker(store,'p',Adapter());b.submit(order(client_order_id='one'),{'custom':{'nested':True}})
    b.amend('one',{'count':'12','price':'.41'})
    b.cancel('one')
    assert store.reserved('p')>0
    b.fill('one','1','.4','.02','fill',{})
    assert next(iter(b.state().lots.values()))['context']=={'custom':{'nested':True}}
    b.finish('one','canceled')
    assert store.reserved('p')==0
