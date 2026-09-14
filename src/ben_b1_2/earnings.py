"""Bounded primary-source earnings evidence for B1.2, never a PIT backfill.

Only independently reviewed release dates enter the fact list. The HTTP cache is
an evidence receipt, not a parser that promotes a successful request to a fact.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import tomllib

import requests

from src.ben_b1.credentials import atomic_json, classify_response
from src.ben_b1.earnings_sources import PAIRS

ROOT = Path(r'C:\Users\benhe\BenAITradingData\ben-b1-2-window-portfolio-20260914')
OLD = Path(r'C:\Users\benhe\BenAITradingData\ben-b1-1-replay-20260914')
SECRETS = Path(r'C:\Users\benhe\OneDrive\Documentos\GITHUB\premarket-radar-ai-m1\.streamlit\secrets.toml')
START, END = '2026-01-02', '2026-03-31'

# Extension is the next consecutive ordinary fiscal release, not the quarter
# end and not the date of an SEC filing. Each source was actually read.
EXTENSIONS = [
 ('MRVL','2026-05-27','https://investor.marvell.com/news-events/press-releases/detail/1023/marvell-technology-inc-reports-first-quarter-of-fiscal-year-2027-financial-results','FY2027 Q1'),
 ('BE','2026-04-28','https://investor.bloomenergy.com/press-releases/press-release-details/2026/Bloom-Energy-Reports-Record-First-Quarter-2026-Results-and-Raises-Full-Year-2026-Guidance/default.aspx','2026 Q1'),
 ('STX','2026-04-28','https://investors.seagate.com/news/news-details/2026/Seagate-Technology-Reports-Fiscal-Third-Quarter-2026-Financial-Results/','FY2026 Q3; NY issuer date, Singapore exhibit dateline April29'),
 ('CRCL','2026-05-11','https://www.circle.com/pressroom/circle-reports-first-quarter-2026-results','2026 Q1'),
 ('NG','2026-04-01','https://www.sec.gov/Archives/edgar/data/1173420/000117184326002143/f8k_040126.htm','FY2026 Q1; Item2.02 expressly says release issued April1'),
 ('FCEL','2026-06-08','https://investor.fce.com/press-releases/press-release-details/2026/FuelCell-Energy-Reports-Second-Fiscal-Quarter-2026-Results-Advances-Data-Center-Power-Strategy/default.aspx','FY2026 Q2'),
 ('GLW','2026-04-28','https://www.corning.com/worldwide/en/about-us/news-events/news-releases/2026/04/corning-announces-strong-first-quarter-2026-financial-results.html','2026 Q1'),
 ('RIVN','2026-04-30','https://www.sec.gov/Archives/edgar/data/1874178/000187417826000033/ex-9911q26rivianearningspr.htm','2026 Q1'),
 ('PLTR','2026-05-04','https://www.sec.gov/Archives/edgar/data/1321655/000132165526000026/pltr-20260504.htm','2026 Q1; Item2.02 expressly says press release issued May4'),
 ('CLSK','2026-05-11','https://investors.cleanspark.com/news/news-details/2026/CleanSpark-Reports-Second-Fiscal-Quarter-2026-Results/default.aspx','FY2026 Q2'),
 ('MSTR','2026-05-05','https://www.strategy.com/press/strategy-announces-first-quarter-2026-financial-results_05-05-2026','2026 Q1'),
 ('ORCL','2026-06-10','https://www.oracle.com/news/announcement/q4fy26-earnings-release-2026-06-10/','FY2026 Q4'),
 ('RXT','2026-05-07','https://ir.rackspace.com/news-releases/news-release-details/rackspace-technology-reports-first-quarter-2026-results','2026 Q1'),
]

# Bounded follow-up order follows the earliest Q1 coarse candidate date, never
# returns. Scope-UNKNOWN SIDU is recorded separately instead of force-qualified.
ADDITIONAL_CHAINS = {
 'AXTI': [
  ('2025-10-30','https://investors.axt.com/Investors/news/news-details/2025/AXT-Inc--Announces-Third-Quarter-2025-Financial-Results/default.aspx','2025 Q3'),
  ('2026-02-19','https://investors.axt.com/Investors/news/news-details/2026/AXT-Inc--Announces-Fourth-Quarter-and-Fiscal-Year-2025-Financial-Results/','2025 Q4'),
  ('2026-04-30','https://investors.axt.com/Investors/news/news-details/2026/AXT-Inc--Announces-First-Quarter-2026-Financial-Results/default.aspx','2026 Q1')],
 'AAOI': [
  ('2025-11-06','https://investors.ao-inc.com/news-releases/news-release-details/applied-optoelectronics-reports-third-quarter-2025-results','2025 Q3'),
  ('2026-02-26','https://investors.ao-inc.com/node/16676','2025 Q4'),
  ('2026-05-07','https://investors.ao-inc.com/node/17011','2026 Q1')],
 'C': [
  ('2025-10-14','https://www.citigroup.com/global/news/press-release/2025/third-quarter-2025-results-and-key-metrics','2025 Q3'),
  ('2026-01-14','https://www.citigroup.com/global/news/press-release/2026/fourth-quarter-full-year-2025-results-key-metrics','2025 Q4'),
  ('2026-04-14','https://www.citigroup.com/global/news/press-release/2026/print/first-quarter-2026-results-key-metrics','2026 Q1')],
 'LITE': [
  ('2025-11-04','https://investor.lumentum.com/financial-news-releases/news-details/2025/Lumentum-Announces-First-Quarter-of-Fiscal-Year-2026-Financial-Results/','FY2026 Q1'),
  ('2026-02-03','https://investor.lumentum.com/financial-news-releases/news-details/2026/Lumentum-Announces-Second-Quarter-of-Fiscal-Year-2026-Financial-Results/default.aspx','FY2026 Q2'),
  ('2026-05-05','https://investor.lumentum.com/financial-news-releases/news-details/2026/Lumentum-Announces-Third-Quarter-of-Fiscal-Year-2026-Financial-Results/default.aspx','FY2026 Q3')],
 'REZI': [
  ('2025-11-05','https://www.sec.gov/Archives/edgar/data/1740332/000174033225000032/rezi-20251105.htm','2025 Q3'),
  ('2026-02-24','https://investor.resideo.com/news/news-details/2026/Resideo-Announces-Fourth-Quarter-and-Full-Year-2025-Financial-Results-and-Initiates-2026-Outlook/default.aspx','2025 Q4'),
  ('2026-05-12','https://investor.resideo.com/financials/quarterly-results/','2026 Q1; issuer full release text dated May12')],
 'NVDA': [
  ('2025-11-19','https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-third-quarter-fiscal-2026','FY2026 Q3'),
  ('2026-02-25','https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-fourth-quarter-and-fiscal-2026','FY2026 Q4'),
  ('2026-05-20','https://nvidianews.nvidia.com/news/nvidia-announces-financial-results-for-first-quarter-fiscal-2027','FY2027 Q1')],
 'TSLA': [
  ('2025-10-22','https://ir.tesla.com/press?page=2','2025 Q3; issuer index explicitly states results released'),
  ('2026-01-28','https://ir.tesla.com/press-release/tesla-releases-fourth-quarter-and-full-year-2025-financial-results','2025 Q4'),
  ('2026-04-22','https://ir.tesla.com/press-release/tesla-releases-first-quarter-2026-financial-results','2026 Q1')],
 'WULF': [
  ('2025-11-10','https://investors.terawulf.com/news-events/press-releases/detail/126/terawulf-reports-third-quarter-2025-results','2025 Q3'),
  ('2026-02-26','https://investors.terawulf.com/news-events/press-releases/detail/132/terawulf-reports-fourth-quarter-and-full-year-2025-results','2025 Q4'),
  ('2026-04-14','https://investors.terawulf.com/sec-filings/all-sec-filings/content/0001104659-26-043279/tm2611661d4_ex99-1.htm','PRELIMINARY_2026_Q1'),
  ('2026-05-08','https://investors.terawulf.com/news-events/press-releases/detail/140/terawulf-reports-first-quarter-2026-results','2026 Q1')],
 'NBIS': [
  ('2025-11-11','https://nebius.com/newsroom/nebius-reports-third-quarter-2025-financial-results','2025 Q3'),
  ('2026-02-12','https://nebius.com/newsroom/nebius-reports-fourth-quarter-and-full-year-2025-financial-results','2025 Q4'),
  ('2026-05-13','https://nebius.com/newsroom/nebius-reports-first-quarter-2026-financial-results','2026 Q1')],
 'RKLB': [
  ('2025-11-10','https://investors.rocketlabcorp.com/news-releases/news-release-details/rocket-lab-announces-third-quarter-2025-financial-results-posts','2025 Q3'),
  ('2026-02-26','https://investors.rocketlabcorp.com/news-releases/news-release-details/rocket-lab-announces-fourth-quarter-and-full-year-2025-financial','2025 Q4'),
  ('2026-05-07','https://investors.rocketlabcorp.com/news-releases/news-release-details/rocket-lab-announces-first-quarter-2026-financial-results','2026 Q1')],
 'OKLO': [
  ('2025-11-11','https://oklo.com/investors/financials/quarterly-results/default.aspx','2025 Q3; issuer full release text says today November11'),
  ('2026-03-17','https://oklo.com/investors/financials/quarterly-results/default.aspx','2025 Q4; issuer full release text says today March17'),
  ('2026-05-12','https://oklo.com/newsroom/oklo-publishes-first-quarter-2026-financial-results-and-business-update','2026 Q1')],
}

ADDITIONAL_PLANS = [
 ('AAOI','2026-02-05','2026-02-26','https://investors.ao-inc.com/node/16541'),
 ('C','2026-01-05','2026-01-14','https://www.citigroup.com/global/news/press-release/2026/citi-fourth-quarter-2025-earnings-call'),
 ('REZI','2026-02-03','2026-02-24','https://investor.resideo.com/news/news-details/2026/Resideo-To-Release-Fourth-Quarter-and-Full-Year-2025-Financial-Results-on-February-24-2026/default.aspx'),
]


def utc():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def check_existing_credential(output=ROOT, secrets=SECRETS, environment=None):
    """Recheck exactly the previously identified entry, no disk/key scan."""
    environment = os.environ if environment is None else environment
    report = {'checked_at': utc(), 'previous_report': str(OLD/'CREDENTIAL_CAPABILITY_REDACTED.json'),
              'previous_report_sha256': sha(OLD/'CREDENTIAL_CAPABILITY_REDACTED.json'),
              'exact_config_path': str(secrets), 'field': 'FINNHUB_API_KEY', 'credential_value_logged': False,
              'config_exists': Path(secrets).is_file(), 'config_error': None,
              'scope': 'ONLY_PREVIOUSLY_IDENTIFIED_ACTIVE_SECRETS_AND_EXACT_PROCESS_ENVIRONMENT_FIELD',
              'permission_calls': [], 'purchases': 0}
    key = ''
    if Path(secrets).is_file():
        try:
            key = str(tomllib.loads(Path(secrets).read_text(encoding='utf-8-sig')).get('FINNHUB_API_KEY') or '').strip()
        except (ValueError, OSError) as exc:
            report['config_error'] = type(exc).__name__
    report['secrets_field_nonempty'] = bool(key)
    report['process_environment_field_nonempty'] = bool(environment.get('FINNHUB_API_KEY', '').strip())
    if not key:
        key = environment.get('FINNHUB_API_KEY', '').strip()
    report['credential_present'] = bool(key)
    tests = [('recent_calendar','calendar/earnings',{'symbol':'MRVL','from':'2026-08-01','to':'2026-09-30'}),
             ('q1_historical_calendar','calendar/earnings',{'symbol':'MRVL','from':START,'to':END}),
             ('older_historical_calendar','calendar/earnings',{'symbol':'MRVL','from':'2018-02-01','to':'2018-03-31'}),
             ('actual_earnings','stock/earnings',{'symbol':'MRVL','limit':12})]
    for label, endpoint, params in tests:
        row = {'label':label,'endpoint':endpoint,'parameters':params}
        if not key:
            row['status'] = 'NOT_EXECUTED_CREDENTIAL_MISSING'
        else:
            row['requested_at'] = utc()
            try:
                response = requests.get('https://finnhub.io/api/v1/'+endpoint, params=params,
                                        headers={'X-Finnhub-Token':key}, timeout=(10,25))
                payload = response.json()
                row.update(http_status=response.status_code, received_at=utc(), status=classify_response(response.status_code,payload))
                # No response error strings: some providers may echo a secret.
                if response.ok and not (isinstance(payload,dict) and payload.get('error')):
                    rows = payload.get('earningsCalendar',[]) if isinstance(payload,dict) else payload
                    row['rows'] = len(rows) if isinstance(rows,list) else None
                    row['available_row_fields'] = sorted(rows[0]) if isinstance(rows,list) and rows and isinstance(rows[0],dict) else []
                    row['plan_vintage_verified'] = False
                    row['period_is_not_release_date'] = endpoint == 'stock/earnings'
                    atomic_json(Path(output)/'earnings'/'private_api'/f'{label}.json', {'receipt':row,'payload':payload})
            except (requests.RequestException, ValueError) as exc:
                row.update(status='NETWORK_OR_RESPONSE_ERROR',error_class=type(exc).__name__)
        report['permission_calls'].append(row)
    report['status'] = 'CREDENTIAL_MISSING' if not key else 'EXECUTED_SEE_SEPARATE_ENDPOINT_RESULTS'
    report['permission_status'] = 'NOT_TESTED' if not key else 'SEE_ENDPOINT_RESULTS'
    report['minimal_user_action'] = 'If Finnhub API access is needed, add top-level FINNHUB_API_KEY to the exact_config_path; existing other fields remain unchanged.'
    report['empty_response_means_no_earnings'] = False
    atomic_json(Path(output)/'earnings'/'CREDENTIAL_RECHECK_REDACTED.json',report)
    return report


def source_receipt(url, output=ROOT):
    """Reuse the old immutable receipt when present; otherwise one bounded GET."""
    key = hashlib.sha256(url.encode()).hexdigest()
    prior = OLD/'earnings'/'private_source_pages'/f'{key}.json'
    target = Path(output)/'earnings'/'private_source_pages'/f'{key}.json'
    if target.exists():
        return read(target)
    if prior.exists():
        result = {**read(prior), 'reuse_path':str(prior),'reuse_sha256':sha(prior),'reuse_verified_at':utc()}
        atomic_json(target,result)
        return result
    result = {'url':url,'request_started_at':utc(),'historical_network_received_at':'UNKNOWN',
              'content_verification':'SEPARATE_MANUAL_PRIMARY_SOURCE_REVIEW_NOT_INFERRED_FROM_HTTP_STATUS'}
    try:
        response = requests.get(url, timeout=(10,25), headers={'User-Agent':'BenAITradingResearch/1.2 (personal financial research)'})
        result.update(source_received_at=utc(),http_status=response.status_code,
                      status='ACCESS_OK' if response.ok else 'HTTP_ERROR',sha256=hashlib.sha256(response.content).hexdigest(),bytes=len(response.content))
        target.parent.mkdir(parents=True,exist_ok=True)
        target.with_suffix('.html').write_bytes(response.content)
    except requests.RequestException as exc:
        result.update(status='REQUEST_FAILED',error_class=type(exc).__name__,source_received_at=None)
    atomic_json(target,result)
    return result


def build_primary(output=ROOT):
    output = Path(output)
    inherited = read(OLD/'earnings'/'PRIMARY_EARNINGS.json')
    extension = {x[0]:x for x in EXTENSIONS}
    facts, receipts = [], {}
    for previous in inherited['facts']:
        symbol = previous['symbol']
        if not previous.get('coverage_complete') or symbol not in extension:
            facts.append({**previous,'scope':'UNCHANGED_INHERITED_C_UNKNOWN', 'inherited_fact_sha256':hashlib.sha256(json.dumps(previous,sort_keys=True).encode()).hexdigest()})
            continue
        _, final_date, final_url, sequence = extension[symbol]
        releases = [{'release_date_ny':previous['previous_release_date_ny'],'source':previous['previous_source'],'fiscal_label':previous['fiscal_sequence'].split(' -> ')[0]},
                    {'release_date_ny':previous['next_release_date_ny'],'source':previous['next_source'],'fiscal_label':previous['fiscal_sequence'].split(' -> ')[-1]},
                    {'release_date_ny':final_date,'source':final_url,'fiscal_label':sequence}]
        for release in releases:
            release.update(actual_release_time='UNKNOWN',actual_release_time_precision='NY_DATE_ONLY',historical_network_received_at='UNKNOWN',verified_as='ACTUAL_EARNINGS_RELEASE_DATE')
            receipts[release['source']] = source_receipt(release['source'], output)
        facts.append({'symbol':symbol,'tier':'B_RETROSPECTIVE_EARNINGS_EXCLUSION','coverage_complete':True,
                      'coverage_start':START,'coverage_end':final_date,'releases':releases,
                      'actual_release_time':'UNKNOWN','historical_network_received_at':'UNKNOWN','reviewed_at':utc(),
                      'completeness_basis':'Consecutive ordinary fiscal releases covering Q1 through the next actual release boundary; not an exhaustive unplanned financial update audit.',
                      'ordinary_calendar_complete':True,'ad_hoc_financial_update_completeness':'NOT_EXHAUSTIVELY_VERIFIED',
                      'q1_diagnostic_only':True,'pit_plan_availability':'NOT_ESTABLISHED_BY_ACTUAL_RELEASES',
                      'limitations':previous.get('limitations','')})
        # Persist a recovery snapshot after each completed symbol.
        atomic_json(output/'earnings'/'BUILD_PROGRESS.json',{'at':utc(),'facts_completed':[x['symbol'] for x in facts]})
    result={'version':'B12_PRIMARY_EARNINGS_V1','created_at':utc(),'scope':{'start':START,'end':END},
            'inherited_source':str(OLD/'earnings'/'PRIMARY_EARNINGS.json'),'inherited_sha256':sha(OLD/'earnings'/'PRIMARY_EARNINGS.json'),
            'facts':facts,'plans':inherited['plans'],'source_receipts':list(receipts.values()),
            'limitations':['B is retrospective actual-date exclusion, never historical knowledge of future release dates.',
                           'Calendar completeness is bounded consecutive ordinary fiscal releases; unplanned financial updates are not exhaustively verified.',
                           'Known MRNA preliminary update and incomplete BMNR release history remain C; absent API rows are never clearance.',
                           'Current download receipt times are distinct from unknown historical network receipt times.']}
    target=output/'earnings'/'PRIMARY_EARNINGS_B12_V1.json'
    if target.exists():
        # An existing evidence version is immutable. A changed curated list needs
        # a new explicit version name, not overwriting the old snapshot.
        return read(target)
    atomic_json(target,result)
    return result


def pair_segments(primary):
    """Adapter-friendly consecutive bounds, retaining the evidence tier."""
    result=[]
    for fact in primary['facts']:
        if not fact.get('coverage_complete'):
            result.append(fact.copy())
            continue
        releases=fact.get('releases',[])
        for previous, following in zip(releases,releases[1:]):
            result.append({**fact,'previous_release_date_ny':previous['release_date_ny'],
                           'next_release_date_ny':following['release_date_ny'],
                           'previous_source':previous['source'],'next_source':following['source'],
                           'coverage_start':max(START,previous['release_date_ny']),
                           'coverage_end':following['release_date_ny']})
    return result


def build_v2(output=ROOT):
    """Extend the evidence version; never mutate first20 or V1 facts."""
    output=Path(output)
    target=output/'earnings'/'PRIMARY_EARNINGS_B12_V2.json'
    if target.exists():
        return read(target)
    prior=read(output/'earnings'/'PRIMARY_EARNINGS_B12_V1.json')
    facts=list(prior['facts'])
    receipts={x['url']:x for x in prior['source_receipts']}
    for symbol, chain in ADDITIONAL_CHAINS.items():
        releases=[]
        for release_date,url,label in chain:
            receipt=source_receipt(url,output)
            receipts[url]=receipt
            releases.append({'release_date_ny':release_date,'source':url,'fiscal_label':label,
                             'event_type':'PRELIMINARY_FINANCIAL_RESULTS' if label.startswith('PRELIMINARY') else 'ORDINARY_QUARTERLY_RESULTS',
                             'actual_release_time':'UNKNOWN','actual_release_time_precision':'NY_DATE_ONLY',
                             'historical_network_received_at':'UNKNOWN','source_received_at':receipt.get('source_received_at'),
                             'verified_as':'ACTUAL_FINANCIAL_RELEASE_DATE'})
        facts.append({'symbol':symbol,'tier':'B_RETROSPECTIVE_EARNINGS_EXCLUSION','coverage_complete':True,
                      'coverage_start':START,'coverage_end':chain[-1][0],'releases':releases,'reviewed_at':utc(),
                      'ordinary_calendar_complete':True,'ad_hoc_financial_update_completeness':'NOT_EXHAUSTIVELY_VERIFIED',
                      'historical_network_received_at':'UNKNOWN','actual_release_time':'UNKNOWN',
                      'completeness_basis':'Consecutive ordinary fiscal results plus specifically identified preliminary release events; bounded Q1 diagnostic only.',
                      'limitations':'WULF April14 preliminary financial results explicitly included before May8 ordinary release.' if symbol=='WULF' else
                                    'TSLA production/delivery volumes and analyst consensus posts are separate operational/analyst disclosures, not company preliminary earnings.' if symbol=='TSLA' else
                                    'No exhaustive unscheduled financial disclosure audit; actual dates do not establish prior plans.'})
        atomic_json(output/'earnings'/'BUILD_PROGRESS_V2.json',{'at':utc(),'facts_completed':[x['symbol'] for x in facts]})
    facts.append({'symbol':'SIDU','tier':'C_UNKNOWN','coverage_complete':False,'coverage_start':None,'coverage_end':None,
                  'reason':'Inherited business scope UNKNOWN. March31 earnings call announcement versus April1 release dateline remains a date/release conflict; call is not substituted for release.',
                  'sources':['https://investors.sidusspace.com/news-events/press-releases/detail/277/sidus-space-to-host-fourth-quarter-and-full-year-2025',
                             'https://investors.sidusspace.com/sec-filings/all-sec-filings/content/0001493152-26-014511/ex99-1.htm'],
                  'historical_network_received_at':'UNKNOWN'})
    plans=list(prior['plans'])
    for symbol,published,released,url in ADDITIONAL_PLANS:
        receipts[url]=source_receipt(url,output)
        plans.append({'symbol':symbol,'publication_date':published,'publication_time':'UNKNOWN',
                      'conservative_known_at_ny':str(date.fromisoformat(published)+timedelta(days=1))+'T00:00:00',
                      'planned_release_date':released,'source':url,'availability_basis':'NEXT_DATE_BOUND_NOT_FABRICATED_PUBLICATION_TIMESTAMP',
                      'historical_network_received_at':'UNKNOWN','source_received_at':receipts[url].get('source_received_at')})
    result={**prior,'version':'B12_PRIMARY_EARNINGS_V2','created_at':utc(),'facts':facts,'plans':plans,
            'source_receipts':list(receipts.values()),'previous_version_sha256':sha(output/'earnings'/'PRIMARY_EARNINGS_B12_V1.json'),
            'additional_review_scope':'Earliest Q1 coarse-candidate order: AXTI, scope-UNKNOWN SIDU (no further qualification), AAOI, C, LITE, REZI, NVDA, TSLA, WULF, NBIS, RKLB, OKLO. No outcomes read.'}
    atomic_json(target,result)
    atomic_json(output/'earnings'/'PAIR_SEGMENTS_B12_V2.json',pair_segments(result))
    capability=read(output/'INPUT_CAPABILITY_EARNINGS.json')
    capability.update(created_at=utc(),primary_evidence=str(target),primary_evidence_sha256=sha(target),
                      B_ordinary_calendar_q1_covered_symbols=[x['symbol'] for x in facts if x.get('coverage_complete')],
                      C_unknown_symbols=[x['symbol'] for x in facts if not x.get('coverage_complete')],A_public_plan_versions=len(plans),
                      previous_capability_version='Original V1 evidence and pair files are preserved')
    atomic_json(output/'INPUT_CAPABILITY_EARNINGS.json',capability)
    return result


def export_coverage(output=ROOT):
    """Read-only capability tables, not trading permission or strategy signals."""
    import pandas as pd
    import pandas_market_calendars as mcal
    from zoneinfo import ZoneInfo
    output=Path(output)
    source=output/'earnings'/'PRIMARY_EARNINGS_B12_V2.json'
    primary=read(source)
    facts={x['symbol']:x for x in primary['facts']}
    scope=pd.read_csv(output/'data'/'ALL66_SCOPE_VERSION.csv')
    schedule=mcal.get_calendar('NYSE').schedule(START,'2026-04-30')
    rows=[]
    daily=[]
    ny=ZoneInfo('America/New_York')
    for identity in scope.to_dict('records'):
        symbol=identity['symbol']; fact=facts.get(symbol,{})
        known=bool(fact.get('coverage_complete'))
        row={'symbol':symbol,'scope_policy':identity['scope_policy'],
             'identity_version':identity['identity_version'],'candidate_retained':True,
             'evidence_tier':'B_RETROSPECTIVE_EARNINGS_EXCLUSION' if known else fact.get('tier','C_UNKNOWN'),
             'B_ordinary_calendar_coverage_start':fact.get('coverage_start'),
             'B_ordinary_calendar_coverage_end':fact.get('coverage_end'),
             'B_q1_calendar_covered':known and fact.get('coverage_start','9999')<=START and fact.get('coverage_end','0000')>=END,
             'actual_release_dates':json.dumps([r['release_date_ny'] for r in fact.get('releases',[])],ensure_ascii=False),
             'actual_release_time_precision':'NY_DATE_ONLY' if known else 'UNKNOWN',
             'original_historical_network_received_at':'UNKNOWN',
             'A_plans_count':sum(p['symbol']==symbol for p in primary['plans']),
             'ad_hoc_financial_update_completeness':'NOT_EXHAUSTIVELY_VERIFIED',
             'unknown_reason':None if known else fact.get('reason') or fact.get('limitations') or 'NO_BOUNDED_PRIMARY_RELEASE_CHAIN_IN_THIS_FINITE_COLLECTION',
             'evidence_file_sha256':sha(source),'coverage_is_not_an_entry_clearance':True}
        rows.append(row)
        for day,session in schedule.iterrows():
            day_text=str(day.date()); moment=(session.market_close.to_pydatetime()+timedelta(minutes=5)).astimezone(ny)
            covered=known and fact['coverage_start']<=day_text<=fact['coverage_end']
            releases=sorted(r['release_date_ny'] for r in fact.get('releases',[]))
            plans=[p for p in primary['plans'] if p['symbol']==symbol and
                   datetime.fromisoformat(p['conservative_known_at_ny']).replace(tzinfo=ny)<=moment and
                   p['planned_release_date']>=day_text]
            daily.append({'symbol':symbol,'trade_date_ny':day_text,'scope_policy':identity['scope_policy'],
                          'phase':'ENTRY_INTERVAL' if day_text<=END else 'EXIT_ONLY_TAIL',
                          'B_date_coverage':'BOUNDED_CALENDAR_AVAILABLE' if covered else 'UNKNOWN',
                          'B_next_actual_release':next((x for x in releases if x>=day_text),None) if covered else None,
                          'B_previous_actual_release':max([x for x in releases if x<day_text],default=None) if covered else None,
                          'A_known_active_plan_dates':json.dumps(sorted(set(p['planned_release_date'] for p in plans))),
                          'A_plan_coverage':'PRIOR_DATED_PLAN_AVAILABLE' if plans else 'UNKNOWN',
                          'A_publication_time_precision':'NEXT_NY_DATE_CONSERVATIVE_BOUND' if plans else 'UNKNOWN',
                          'original_historical_network_received_at':'UNKNOWN',
                          'coverage_is_not_an_entry_clearance':True})
    pd.DataFrame(rows).to_csv(output/'earnings'/'EARNINGS_ALL66_COVERAGE_V2.csv',index=False)
    pd.DataFrame(daily).to_csv(output/'earnings'/'EARNINGS_DAILY_COVERAGE_V2.csv',index=False)
    report='''# B1.2 财报输入核验

本轮沿用原 B1.1 first20 的财报文件，不改写其 A/B/C 判定。共同资金季度回放使用独立的 `PRIMARY_EARNINGS_B12_V2.json`。

已经核实 24 个证券的连续普通季度实际发布日，覆盖 2026-01-02 至 2026-03-31，并保留各自延伸至下一实际发布日的边界。原 13 个为 MRVL、BE、STX、CRCL、NG、FCEL、GLW、RIVN、PLTR、CLSK、MSTR、ORCL、RXT；按最早潜在事件顺序新增 AXTI、AAOI、C、LITE、REZI、NVDA、TSLA、WULF、NBIS、RKLB、OKLO。没有读取这些候选的账户盈亏来挑选证券。

这是 **B 层事后实际日期避让诊断**。连续普通季度链可用于有限的历史工程诊断；并未证明已穷尽所有非定期财务更新，也未证明当时知道未来财报计划。所有证券继续保留 `ad_hoc_financial_update_completeness=NOT_EXHAUSTIVELY_VERIFIED`。实际发布时间未知的记录只有纽约日期精度，不能用电话会时间冒充结果发布时刻。

WULF 的 2026-04-14 初步一季度财务公告已经单列为真实事件，位于 2/26 正式年报与 5/8 正式季报之间；兼容回放分段中它就是下一财报边界，必须参与 B 层退出。NG 4/1 实际发布、C 4/14、TSLA 4/22、BE/STX/GLW 4/28、AXTI 4/30 也保留各自边界。4月退出尾段中，下一真实发布日期跨过 4/30 的股票保留对应覆盖；已到最后已核实实际日后、下一事件未知的日期仍为 UNKNOWN。

MRNA 的已发现初步财务更新处理仍未完成，BMNR 的完整实际发布链未知，SIDU 同时有原范围 UNKNOWN 及 3/31 电话会和 4/1 结果稿日期冲突，均不升为通过。其余没有本轮连续链的候选全部列在 66 行表和逐日表中，UNKNOWN 只阻断相关证券/日期；不得把这些缺口隐藏成完整全池绩效。

A 层单独保留原 3 个事前计划，并新增 AAOI 2/5 公布 2/26、C 1/5 公布 1/14、REZI 2/3 公布 2/24 的计划证据。由于页面未核实精确发布时间，known_at 使用公告日之后的下一个纽约自然日 00:00 的保守可得边界，并明确它不是实际历史接收时间。这只是可用计划证据，不自动代表所有恢复/入场条件通过。

只复查了 B1.1 已确认的实际项目 Secrets 文件及准确的进程变量 FINNHUB_API_KEY，仍未发现有效值。Finnhub 四项端点未调用，权限为待验证，不能据此断言套餐拒绝。最少配置动作是在 `C:/Users/benhe/OneDrive/Documentos/GITHUB/premarket-radar-ai-m1/.streamlit/secrets.toml` 顶层填写 `FINNHUB_API_KEY`，保留已有字段。本轮没有购买服务、输出凭证或把空响应当作无财报。

本次抓取收据、旧收据复用路径及 SHA256 保存在来源清单中；部分站点直接请求失败时保留 HTTP_ERROR/REQUEST_FAILED。事实另外依据实际读取的发行人网页或 SEC Item 2.02/发行人附件中的明确发布日期核验，HTTP 200 本身不构成内容核验。完整网页缓存不属于验收包白名单；验收包只需事实、来源地址/收据哈希、覆盖表和本说明。

覆盖表是输入能力说明，不是策略信号或交易许可。前三交易日、公告后恢复、买卖及现金规则仍由原冻结内核逐时执行。
'''
    (output/'earnings'/'EARNINGS_EVIDENCE_V2.md').write_text(report,encoding='utf-8')
    summary={'at':utc(),'all66_rows':len(rows),'daily_rows':len(daily),'q1_covered_count':sum(r['B_q1_calendar_covered'] for r in rows),
             'source_version':primary['version'],'source_sha256':sha(source),'source_receipts':len(primary['source_receipts'])}
    atomic_json(output/'earnings'/'COVERAGE_EXPORT_V2.json',summary)
    return summary


def main():
    credential = check_existing_credential()
    primary = build_primary()
    atomic_json(ROOT/'earnings'/'PAIR_SEGMENTS_B12_V1.json',pair_segments(primary))
    capability={'created_at':utc(),'credential':credential,'primary_evidence':str(ROOT/'earnings'/'PRIMARY_EARNINGS_B12_V1.json'),
                'primary_evidence_sha256':sha(ROOT/'earnings'/'PRIMARY_EARNINGS_B12_V1.json'),
                'B_ordinary_calendar_q1_covered_symbols':[x['symbol'] for x in primary['facts'] if x.get('coverage_complete')],
                'C_unknown_symbols':[x['symbol'] for x in primary['facts'] if not x.get('coverage_complete')],
                'A_public_plan_versions':len(primary['plans']),'limitations':primary['limitations'],
                'other_candidates_status':'UNKNOWN_UNTIL_SEPARATE_BOUNDED_PRIMARY_REVIEW'}
    atomic_json(ROOT/'INPUT_CAPABILITY_EARNINGS.json',capability)
    print(json.dumps({'credential_status':credential['status'],'B_symbols':capability['B_ordinary_calendar_q1_covered_symbols'],'C_symbols':capability['C_unknown_symbols']},ensure_ascii=False))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--v2',action='store_true')
    parser.add_argument('--coverage',action='store_true')
    args=parser.parse_args()
    if args.coverage:
        print(json.dumps(export_coverage()))
    elif args.v2:
        r=build_v2();print(json.dumps({'version':r['version'],'facts':len(r['facts']),'plans':len(r['plans'])}))
    else:
        main()
