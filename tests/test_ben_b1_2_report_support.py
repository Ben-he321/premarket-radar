"""Small read-only reporting fixtures, not new historical accounts."""
import pytest
from src.ben_b1_2_continuation.report_support import coverage_summary,verified_benchmarks
from src.ben_b1_2_continuation.runtime import write,sha

def test_coverage_deduplicates_samples_but_keeps_status_and_unknown_distinctions():
    rows=[{'symbol':'BE','day':'2026-01-22','purpose':'ENTRY','status':'ACCESS_OK','complete':True,'rows':8}]*2
    rows += [{'symbol':'BE','day':'2026-01-22','purpose':'ENTRY','status':'NOT_REQUESTED_EARNINGS_UNVERIFIABLE_THIS_WINDOW','complete':None,'rows':None},
        {'symbol':'NVDA','day':'2026-01-22','purpose':'ENTRY','status':'EMPTY_RESPONSE','complete':True,'rows':0},
        {'symbol':'NVDA','day':'2026-01-23','purpose':'HELD_RTH','status':'ACCESS_OK','complete':True,'rows':90000}]
    proof=coverage_summary(rows,'2026-01-22')
    assert proof['durable_records']==4 and proof['unique_symbol_day_purpose']==2
    assert len(proof['not_yet_durable_records'])==1
    groups={r['status']:r for r in proof['groups']}
    assert groups['ACCESS_OK']['unique_symbol_day_purpose']==1 and groups['ACCESS_OK']['records']==2
    assert groups['EMPTY_RESPONSE']['reported_rows']==0
    assert groups['NOT_REQUESTED_EARNINGS_UNVERIFIABLE_THIS_WINDOW']['complete'] is None

def fixture(tmp_path,case):
    rows=[]
    for symbol in ('QQQ','SPY'):
        rows.append({'symbol':symbol,'start':'2026-01-02','end':'2026-01-02','initial_capital_usd':5500,
            'status':'COMPUTED_REAL_INPUT_MODEL_EXECUTION','issues':[],'end_equity_usd':5490})
        p=tmp_path/symbol/'daily.csv';p.parent.mkdir()
        p.write_text('trade_date,symbol,equity_usd\n2026-01-02,'+symbol+',5490\n',encoding='utf-8')
    if case=='capital':rows[0]['initial_capital_usd']=5000
    if case=='date':rows[0]['end']='2026-01-05'
    if case=='duplicate':rows[0]['symbol']='SPY'
    if case=='session':(tmp_path/'QQQ/daily.csv').write_text('trade_date,symbol,equity_usd\n2026-01-05,QQQ,5490\n')
    write(tmp_path/'SUMMARY.json',rows)
    files=[tmp_path/'SUMMARY.json',tmp_path/'QQQ/daily.csv',tmp_path/'SPY/daily.csv']
    write(tmp_path/'COMPLETE.json',{'outputs':[{'path':str(p.relative_to(tmp_path)),'sha256':sha(p)} for p in files]})
    if case=='hash':(tmp_path/'SPY/daily.csv').write_text('changed')

def test_benchmark_mapping_does_not_depend_on_summary_array_order(tmp_path):
    fixture(tmp_path,'pass');by_symbol,proof=verified_benchmarks(tmp_path,'2026-01-02','2026-01-02')
    assert by_symbol['SPY']['symbol']=='SPY' and proof['status']=='PASS'

@pytest.mark.parametrize('case',['capital','date','duplicate','session','hash'])
def test_benchmark_cannot_claim_completion_with_wrong_boundary_or_hash(tmp_path,case):
    fixture(tmp_path,case)
    with pytest.raises(ValueError):verified_benchmarks(tmp_path,'2026-01-02','2026-01-02')
