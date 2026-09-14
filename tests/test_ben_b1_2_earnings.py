"""Offline evidence boundary tests; no synthetic research files or API calls."""
import json

from src.ben_b1_2.earnings import check_existing_credential, pair_segments


def test_missing_exact_finnhub_field_does_not_call_network(tmp_path, monkeypatch):
    secrets=tmp_path/'secrets.toml'
    secrets.write_text('ALPACA_API_KEY="unit_test_secret_never_logged"\n',encoding='utf-8')
    def forbidden(*args,**kwargs):
        raise AssertionError('Missing credential must not make an HTTP request')
    monkeypatch.setattr('src.ben_b1_2.earnings.requests.get',forbidden)
    report=check_existing_credential(tmp_path,secrets,{})
    assert report['status']=='CREDENTIAL_MISSING'
    assert report['permission_status']=='NOT_TESTED'
    assert all(x['status']=='NOT_EXECUTED_CREDENTIAL_MISSING' for x in report['permission_calls'])
    assert 'unit_test_secret_never_logged' not in json.dumps(report)
    assert report['exact_config_path']==str(secrets)


def test_actual_dates_remain_retrospective_and_do_not_promote_unknown():
    actual={'symbol':'KNOWN','tier':'B_RETROSPECTIVE_EARNINGS_EXCLUSION','coverage_complete':True,
            'releases':[{'release_date_ny':'2025-11-01','source':'issuer/q3'},
                        {'release_date_ny':'2026-02-01','source':'issuer/q4'},
                        {'release_date_ny':'2026-05-01','source':'issuer/q1'}]}
    unknown={'symbol':'UNKNOWN','tier':'C_UNKNOWN','coverage_complete':False,'coverage_start':None,'coverage_end':None}
    rows=pair_segments({'facts':[actual,unknown]})
    assert len(rows)==3
    assert rows[0]['coverage_start']=='2026-01-02'
    assert rows[0]['coverage_end']=='2026-02-01'
    assert rows[1]['coverage_start']=='2026-02-01'
    assert rows[1]['next_release_date_ny']=='2026-05-01'
    assert all(x['tier']=='B_RETROSPECTIVE_EARNINGS_EXCLUSION' for x in rows[:2])
    assert rows[2]==unknown
    assert 'conservative_known_at_ny' not in rows[0]


def test_preliminary_release_is_a_separate_exclusion_boundary():
    fact={'symbol':'WULF','tier':'B_RETROSPECTIVE_EARNINGS_EXCLUSION','coverage_complete':True,
          'releases':[{'release_date_ny':'2026-02-26','source':'q4'},
                      {'release_date_ny':'2026-04-14','source':'preliminary','event_type':'PRELIMINARY_FINANCIAL_RESULTS'},
                      {'release_date_ny':'2026-05-08','source':'ordinary'}]}
    rows=pair_segments({'facts':[fact]})
    assert rows[0]['next_release_date_ny']=='2026-04-14'
    assert rows[1]['previous_release_date_ny']=='2026-04-14'
    assert rows[1]['next_release_date_ny']=='2026-05-08'
