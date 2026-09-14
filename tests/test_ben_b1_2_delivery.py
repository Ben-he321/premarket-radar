"""Temporary-directory delivery safety and truthful-status boundary fixtures.

No production bundle/report is generated, no market data or real credentials
are read, and no strategy computation is performed by these tests.
"""
import json
from pathlib import Path
import zipfile

import pytest

from src.ben_b1_2 import delivery


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding="utf-8")
    return path


def test_activity_uses_ny_quarter_dates_and_excludes_warmup_and_tail(tmp_path):
    """Mock exported fills only; this does not run a strategy or market client."""
    put(tmp_path / 'fills.csv',
        'at,symbol,side,quantity\n'
        '2026-01-01T21:05:00+00:00,MOCK,BUY,99\n'
        '2026-01-02T21:05:00+00:00,MOCK,BUY,2\n'
        '2026-04-01T00:10:00.123456789+00:00,MOCK,SELL,1\n'
        '2026-04-01T14:00:00Z,MOCK,SELL,1\n')
    result = delivery.audit_account(tmp_path)
    assert result['trading_activity'] == [{'symbol': 'MOCK', 'buy_dates': ['2026-01-02'],
        'sell_dates': ['2026-03-31'], 'buy_shares': 2, 'sell_shares': 1}]
    assert result['quarter_end_equity'] is None
    assert result['actually_completed'] is False


@pytest.fixture
def isolated_delivery(tmp_path, monkeypatch):
    for field in ("ROOT", "REPO", "OLD_REPO", "B11", "B1"):
        folder = tmp_path / field.lower()
        folder.mkdir()
        monkeypatch.setattr(delivery, field, folder)
    put(delivery.ROOT / "BEN_B1_2_RESULTS.md", "Explicit MOCK packaging test. No research result.")
    return delivery.ROOT


@pytest.mark.parametrize("relative", [
    "market_dump.json",
    "engineering/database_dump.txt",
    "portfolio/base_v1/B12_Q0_P50_A_base_v1/raw_quotes.csv",
    "portfolio/base_v1/B12_Q0_P50_A_base_v1/credentials.json",
    "earnings/finnhub_credentials.json",
    "samples/sample_test/tier_A/sample_MOCK/checkpoint_backup.json",
])
def test_unknown_text_data_and_secret_dumps_cannot_enter_whitelist(isolated_delivery, relative):
    put(isolated_delivery / relative, "MOCK_DISALLOWED_PAYLOAD_ONLY")
    try:
        result = delivery.package("sample_test", "base_v1")
    except ValueError as exc:
        # Fail-closed rejection is also valid; packaging must never claim safe
        # inclusion merely because the dump has a JSON/CSV/TXT extension.
        assert any(word in str(exc).upper() for word in ("FORBIDDEN", "SECRET", "UNEXPECTED", "WHITELIST"))
        return
    with zipfile.ZipFile(result["zip"]) as bundle:
        assert relative not in bundle.namelist()
        assert all(b"MOCK_DISALLOWED_PAYLOAD_ONLY" not in bundle.read(name) for name in bundle.namelist())


def test_explicit_evidence_included_but_raw_db_and_configuration_excluded(isolated_delivery):
    account = "portfolio/base_v1/B12_Q0_P50_A_base_v1/"
    put(isolated_delivery / (account + "FINAL_ACCOUNT.json"), {"status": "MOCK_NOT_REAL_RESEARCH"})
    put(isolated_delivery / (account + "fills.csv"), "side,quantity\nBUY,1\n")
    for name in ("replay_archive.sqlite", "database.db", "bars.parquet", "secrets.toml", ".env"):
        put(isolated_delivery / (account + name), "MOCK_PRIVATE_NEVER_EXPORT")
    result = delivery.package("sample_test", "base_v1")
    with zipfile.ZipFile(result["zip"]) as bundle:
        names = bundle.namelist()
        assert "BEN_B1_2_RESULTS.md" in names
        assert account + "FINAL_ACCOUNT.json" in names
        assert account + "fills.csv" in names
        assert not any(name.endswith((".sqlite", ".db", ".parquet", ".toml", ".env")) for name in names)
        assert bundle.testzip() is None


def test_missing_quarter_evidence_does_not_claim_zero_actual_costs(tmp_path):
    row = delivery.audit_account(tmp_path)
    assert row["actually_completed"] is False
    assert row["quarter_end_equity"] is None
    assert row["commission_paid_through_quarter"] is None
    assert row["friction_paid_through_quarter"] is None


def partial_fixture(root, account):
    path=root/'partial_evidence'/'MOCK_ONLY'
    manifest={'status':'PARTIAL_CHECKPOINT_EVIDENCE_NOT_COMPLETED','source_checkpoint':str(account/'checkpoint.json'),
        'checkpoint_wrapper_hash_verified':True,'source_unchanged_during_extraction':True,'events_run':0,
        'full_interval_completed':False,'synthetic_fixture':False,'saved_config':{'run_id':account.name},
        'saved_interim_cash_not_terminal_result':4200,'files':[],
        'recovery_validation':'NOT_PERFORMED','engine_restored':False,'sqlite_full_chain_verified':False}
    # Handwritten packaging mock; synthetic_fixture=False only exercises the
    # real-evidence attestation gate. No research checkpoint or prices are read.
    for name in delivery.PARTIAL_TABLE_NAMES:
        content='at,symbol,side,quantity\n2026-01-02T21:05:03Z,MOCK,BUY,2\n' if name=='fills' else 'mock_field\n'
        file=put(path/(name+'.csv'),content)
        manifest['files'].append({'file':file.name,'sha256':delivery.sha(file),'rows':1 if name=='fills' else 0})
    put(path/'PARTIAL_EVIDENCE.json',manifest)
    return path,manifest


def test_partial_finance_is_visible_without_terminal_or_recovery_claim(isolated_delivery):
    account=isolated_delivery/'portfolio/base_v1/B12_Q0_P50_B_base_v1'
    put(account/'progress.json',{'processed_day':'2026-01-02'})
    partial,_=partial_fixture(isolated_delivery,account)
    row=delivery.audit_account(account,partial)
    assert row['status']=='PARTIAL_CHECKPOINT_EVIDENCE_NOT_COMPLETED'
    assert row['traded_symbols']==['MOCK']
    assert row['partial_evidence']['saved_interim_cash_not_terminal_result']==4200
    assert row['quarter_end_equity'] is None and row['quarter_profit'] is None
    assert row['actually_completed'] is False and row['recovery']=='NOT_VERIFIED'
    assert row['one_shared_initial_5500'] is None
    assert not (account/'FINAL_ACCOUNT.json').exists()


@pytest.mark.parametrize('defect',['altered_csv','wrong_run','synthetic','missing_table'])
def test_partial_invalid_attestation_refuses_report(isolated_delivery,defect):
    account=isolated_delivery/'portfolio/base_v1/B12_Q0_P50_B_base_v1'
    path,manifest=partial_fixture(isolated_delivery,account)
    if defect=='altered_csv':put(path/'fills.csv','ALTERED')
    elif defect=='wrong_run':manifest['saved_config']['run_id']='WRONG'
    elif defect=='synthetic':manifest['synthetic_fixture']=True
    else:manifest['files'].pop()
    put(path/'PARTIAL_EVIDENCE.json',manifest)
    with pytest.raises(ValueError,match='PARTIAL_'):delivery.audit_account(account,path)


def test_only_indexed_partial_manifest_and_exact_financial_tables_packaged(isolated_delivery):
    account=isolated_delivery/'portfolio/base_v1/B12_Q0_P50_B_base_v1'
    path,_=partial_fixture(isolated_delivery,account)
    put(path/'raw_quotes.csv','PRIVATE_MOCK_QUOTES')
    put(path/'checkpoint.json','PRIVATE_MOCK_CHECKPOINT')
    put(isolated_delivery/'partial_evidence/UNINDEXED/fills.csv','NOT_SELECTED')
    put(isolated_delivery/'PARTIAL_EXPORTS.json',{'accounts':{account.name:str(path)}})
    result=delivery.package('sample_test','base_v1')
    with zipfile.ZipFile(result['zip']) as bundle:
        names=bundle.namelist()
        assert 'partial_evidence/MOCK_ONLY/PARTIAL_EVIDENCE.json' in names
        assert 'partial_evidence/MOCK_ONLY/fills.csv' in names
        assert not any('raw_quotes' in name or 'checkpoint.json' in name or 'UNINDEXED' in name for name in names)


def test_nonexistent_fixed_account_is_not_run_and_cash_not_invented(tmp_path):
    row=delivery.audit_account(tmp_path/'NEVER_STARTED')
    assert row['status']=='NOT_RUN' and row['processed_through'] is None
    assert row['quarter_cash'] is None and row['quarter_end_equity'] is None


@pytest.mark.parametrize('values,expected',[
    ([True,True,True,True],True),([True,True,True,None],None),
    ([True,False,True,None],False),([True,True,True],None)])
def test_shared_capital_attestation_keeps_unverified_distinct_from_disproved(values,expected):
    assert delivery.shared_capital_evidence([{'one_shared_initial_5500':v} for v in values]) is expected


def test_interim_close_is_dated_separately_and_stale_marks_never_become_quarter_nav(tmp_path):
    put(tmp_path/'CLOSE_VALUATIONS.json',[
        {'trade_date':'2026-01-06','cash':25,'market_value':5550,'net_equity':5575,
         'valuation_status':'STALE_MARK_UNKNOWN','missing_current_marks':['MOCK']},
        {'trade_date':'2026-01-05','cash':12,'market_value':5400,'net_equity':5412,
         'valuation_status':'CURRENT_MARKS','missing_current_marks':[]}])
    row=delivery.audit_account(tmp_path)
    latest=row['latest_saved_close_valuation']
    assert latest['trade_date']=='2026-01-06' and latest['cash']==25
    assert latest['net_equity'] is None and latest['market_value'] is None
    assert row['quarter_end_equity'] is None and row['quarter_max_drawdown'] is None
    assert json.loads((tmp_path/'CLOSE_VALUATIONS.json').read_text())[0]['net_equity']==5575


@pytest.mark.parametrize('defect',['stale','duplicate','missing','unsorted'])
def test_quarter_drawdown_requires_unique_complete_calendar_and_current_prices(tmp_path,defect):
    dates=[str(d.date()) for d in delivery.mcal.get_calendar('NYSE').schedule(delivery.START,delivery.END).index]
    rows=[{'trade_date':d,'net_equity':5500-i,'valuation_status':'CURRENT_MARKS','missing_current_marks':[]} for i,d in enumerate(dates)]
    if defect=='stale':rows[5]['valuation_status']='STALE_MARK_UNKNOWN'
    elif defect=='duplicate':rows[5]=dict(rows[4])
    elif defect=='missing':rows.pop(5)
    else:rows.reverse()
    put(tmp_path/'CLOSE_VALUATIONS.json',rows)
    before=(tmp_path/'CLOSE_VALUATIONS.json').read_bytes()
    result=delivery.audit_account(tmp_path)
    assert result['quarter_missing_mark_days']==(1 if defect=='stale' else 0)
    assert result['quarter_calendar_complete']==(defect in ['stale','unsorted'])
    if defect=='unsorted':assert result['quarter_max_drawdown']==pytest.approx(5440/5500-1)
    else:assert result['quarter_max_drawdown'] is None
    assert (tmp_path/'CLOSE_VALUATIONS.json').read_bytes()==before


@pytest.mark.parametrize('defect',['claims_pass','claims_restore','claims_chain','missing_attestation'])
def test_partial_cannot_claim_database_restore_or_recovery(isolated_delivery,defect):
    account=isolated_delivery/'portfolio/base_v1/B12_Q0_P50_B_base_v1'
    path,manifest=partial_fixture(isolated_delivery,account)
    if defect=='claims_pass':manifest['recovery_validation']='PASS'
    elif defect=='claims_restore':manifest['engine_restored']=True
    elif defect=='claims_chain':manifest['sqlite_full_chain_verified']=True
    else:manifest.pop('recovery_validation')
    put(path/'PARTIAL_EVIDENCE.json',manifest)
    with pytest.raises(ValueError,match='PARTIAL_MUST_NOT_CLAIM_RECOVERY'):
        delivery.audit_account(account,path)


def test_partial_or_failed_final_file_is_not_completed_account(tmp_path):
    put(tmp_path / "FINAL_ACCOUNT.json", {"status": "REPLAY_FAILED", "processed_through": "2026-01-05",
                                        "actually_executed": True, "error_count": 1})
    row = delivery.audit_account(tmp_path)
    assert row["actually_completed"] is False


def test_tail_stale_mark_is_unknown_even_if_ledger_has_fallback_value(tmp_path):
    put(tmp_path / "FINAL_ACCOUNT.json", {"status": "ENGINE_EXECUTED_WITH_MODEL_FILLS", "processed_through": delivery.TAIL_END,
        "actually_executed": True, "error_count": 0, "portfolio_not_stitched": True,
        "latest_equity": {"cash": 100., "net_equity": 6000., "valuation_status": "STALE_MARK_UNKNOWN", "missing_current_marks": ["MOCK"]}})
    row = delivery.audit_account(tmp_path)
    assert row["tail_equity"] is None
    assert row["tail_cash"] == 100


def test_empty_incomplete_fill_export_does_not_crash_readonly_report(tmp_path):
    put(tmp_path / "fills.csv", "\n")
    row = delivery.audit_account(tmp_path)
    assert row["actually_completed"] is False
    assert row["traded_symbols"] == []


def test_known_quarter_costs_use_snapshot_campaigns_without_tail_charges(tmp_path):
    put(tmp_path / "QUARTER_END_ACCOUNT.json", {"campaigns": {"campaigns": [{"commission": 3., "friction": 1.25}]},
                                               "buy_intents": 1, "buy_fills": 2, "sell_fills": 1})
    put(tmp_path / "QUARTER_END_CLOSE_VALUATION.json", {"net_equity": 5400., "cash": 2000.,
                                                     "valuation_status": "CURRENT_MARKS", "missing_current_marks": []})
    put(tmp_path / "FINAL_ACCOUNT.json", {"campaigns": {"campaigns": [{"commission": 9., "friction": 4.}]},
                                          "processed_through": delivery.TAIL_END, "actually_executed": True, "error_count": 0,
                                          "status": "ENGINE_EXECUTED_WITH_MODEL_FILLS"})
    put(tmp_path / "RECOVERY_IDEMPOTENCY.json", {"status": "PASS"})
    row = delivery.audit_account(tmp_path)
    assert row["commission_paid_through_quarter"] == 3.
    assert row["friction_paid_through_quarter"] == 1.25
    assert row["quarter_profit"] == -100.


@pytest.mark.parametrize("recovery", [None, "FAIL", "PASS"])
def test_recovery_missing_or_failed_never_claims_engineering_complete(tmp_path, recovery):
    put(tmp_path / "FINAL_ACCOUNT.json", {"status": "ENGINE_EXECUTED_NO_QUALIFIED_NATURAL_ENTRY", "processed_through": delivery.TAIL_END,
                                          "actually_executed": True, "error_count": 0, "portfolio_not_stitched": True})
    put(tmp_path / "fills.csv", "fill_id,order_id,symbol,side,quantity,execution_price\n")
    put(tmp_path / "progress.json", {"processed_day": delivery.TAIL_END})
    if recovery is not None:
        put(tmp_path / "RECOVERY_IDEMPOTENCY.json", {"status": recovery})
    row = delivery.audit_account(tmp_path)
    assert row["processed_through"] == delivery.TAIL_END
    assert row["actually_completed"] is (recovery == "PASS")
