"""Synthetic engineering fixtures only; never touch research accounts."""
import copy
import math
import pytest
import pandas as pd
import pandas_market_calendars as mcal
from src.ben_b1.ledger import Ledger
from src.ben_b1_2.compact import stream_hash
from src.ben_b1_2_continuation import report,package


def valuation(day='2026-01-02',equity=5500):
    close=mcal.get_calendar('NYSE').schedule(day,day).iloc[0].market_close
    book=Ledger();book.advance_day(day)
    return {**book.snapshot({}),'trade_date':day,'account_state_cutoff':(close+pd.Timedelta(minutes=15)).isoformat(),
            'price_cutoff':close.isoformat(),'net_equity':equity}


@pytest.mark.parametrize('change',[
    {'valuation_status':'STALE_MARK_UNKNOWN'}, {'missing_current_marks':['NVDA']},
    {'net_equity':float('nan')},{'net_equity':float('inf')},
    {'trade_date':'2026-01-05'},{'price_cutoff':'2026-01-02T20:59:00Z'},
    {'account_state_cutoff':'2026-01-01T21:15:00Z'}])
def test_invalid_current_valuation_never_produces_drawdown(change):
    value={**valuation(),**change}
    assert report.valuation_issues(value,'2026-01-02')
    assert report.calendar_audit([value],'2026-01-02')['maximum_close_drawdown'] is None


def test_calendar_requires_all_unique_sessions_and_sorts_before_drawdown():
    a=valuation('2026-01-02',6000);b=valuation('2026-01-05',5400)
    audit=report.calendar_audit([b,a],'2026-01-05')
    assert audit['status']=='PASS' and audit['maximum_close_drawdown']==pytest.approx(-.1)
    for rows in ([a],[a,a,b]):
        assert report.calendar_audit(rows,'2026-01-05')['maximum_close_drawdown'] is None


def account(tmp_path,monkeypatch,full=True):
    directory=tmp_path/'account';directory.mkdir()
    monkeypatch.setattr(report,'OLD',tmp_path/'old')
    report.write(report.OLD/'data/HISTORY_INPUTS.json',[])
    report.write(directory/'DYNAMIC_INPUT_HASHES.json',{})
    report.write(directory/'RUN_SPEC.json',{'config':{'run_id':'SYNTHETIC_REPORT_TEST'}})
    dates=mcal.get_calendar('NYSE').schedule(report.START,report.TAIL_END).index
    rows=[valuation(str(d.date())) for d in dates]
    q=Ledger();q.advance_day(report.END)
    report.write(directory/'QUARTER_END_LEDGER.json',q.to_dict())
    report.write(directory/'QUARTER_END_CLOSE_VALUATION.json',next(r for r in rows if r['trade_date']==report.END))
    report.write(directory/'CLOSE_VALUATIONS.json',rows if full else rows[-1:])
    tail=Ledger();tail.advance_day(report.TAIL_END)
    cp={'ledger':tail.to_dict(),'state':{'at':rows[-1]['account_state_cutoff'],'b12_completed_day':{'day':report.TAIL_END}}}
    report.write(directory/'checkpoint.json',{'payload':cp,'sha256':stream_hash(cp)})
    return directory


def test_quarter_and_tail_use_distinct_saved_ledgers(tmp_path,monkeypatch):
    path=account(tmp_path,monkeypatch)
    q,_=report.analyze(path,'Q1/B');tail,_=report.analyze(path,'Q1/B',quarter=False)
    assert q['quarter_complete'] and q['latest_completed_day']==report.END
    assert tail['latest_completed_day']==report.TAIL_END and tail['checkpoint_read_only_verification']['hash_verified']
    assert not tail['quarter_complete']
    assert q['cash_bridge']['difference']==tail['profit_partition_difference']==0


def test_quarter_files_do_not_establish_calendar_completion(tmp_path,monkeypatch):
    path=account(tmp_path,monkeypatch,full=False)
    q,_=report.analyze(path,'Q1/B')
    assert not q['quarter_complete'] and q['quarter_end_equity'] is None
    assert q['maximum_close_drawdown'] is None


def test_stale_close_cannot_mix_with_new_checkpoint(tmp_path,monkeypatch):
    path=account(tmp_path,monkeypatch)
    values=report.read(path/'CLOSE_VALUATIONS.json');report.write(path/'CLOSE_VALUATIONS.json',values[:-1])
    tail,_=report.analyze(path,'Q1/B',quarter=False)
    assert tail['latest_completed_close_equity'] is None
    assert 'NO_UNIQUE_SAME_DAY_CHECKPOINT_CLOSE_VALUATION' in tail['valuation_issues']


def test_report_rejects_tampered_wrapper_without_engine_restore(tmp_path,monkeypatch):
    path=account(tmp_path,monkeypatch)
    wrapper=report.read(path/'checkpoint.json');wrapper['payload']['ledger']['state']['cash']=5501
    report.write(path/'checkpoint.json',wrapper)
    with pytest.raises(ValueError,match='WRAPPER_HASH_FAILED'):report.analyze(path,'Q1/B',quarter=False)


def test_final_inputs_rehash_bytes_even_when_size_unchanged(tmp_path,monkeypatch):
    monkeypatch.setattr(package,'ROOT',tmp_path);monkeypatch.setattr(package,'ACCOUNT',tmp_path/'account')
    monkeypatch.setattr(package,'active_gate_path',lambda:tmp_path/'ENGINEERING_GATE.json')
    f=tmp_path/'input.txt';f.write_text('real')
    package.write(package.ACCOUNT/'DYNAMIC_INPUT_HASHES.json',{str(f):package.sha(f)})
    package.write(tmp_path/'ENGINEERING_GATE.json',{'files':{}})
    assert package.recheck_inputs_and_execution()['status']=='PASS'
    f.write_text('fake')
    with pytest.raises(ValueError,match='HASH_MISMATCH'):package.recheck_inputs_and_execution()
    assert package.read(tmp_path/'final/FINAL_INPUT_HASH_RECHECK.json')['status']=='FAIL'


@pytest.mark.parametrize('change',[{'before':'old','after':'old'},{'after':'different'},{'synthetic':True},{'status':'FAIL'}])
def test_stale_or_nonreal_pass_cannot_validate_current_checkpoint(change):
    proof={'wrapper_sha256':'current','hash_verified':True}
    good={'status':'PASS','before':'current','after':'current','synthetic':False}
    assert report.recovery_matches_checkpoint(good,proof)
    assert not report.recovery_matches_checkpoint({**good,**change},proof)
