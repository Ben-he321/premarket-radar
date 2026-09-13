"""Manually reviewed primary release facts for the frozen first-20 sample.

Dates are release dates, never inferred from a financial period or filing date.
Consecutive fiscal releases support bounded tier B only, not a complete eight
month PIT calendar. Current downloads are not original historical receipts.
"""
from __future__ import annotations
from pathlib import Path
import hashlib
import requests
from .b11_runtime import ROOT, utc, read, write

# symbol, previous actual date/source, next actual date/source, fiscal sequence.
PAIRS = [
 ('MRVL','2025-12-02','https://investor.marvell.com/news-events/press-releases/detail/999/marvell-technology-inc-reports-third-quarter-of-fiscal-year-2026-financial-results',
  '2026-03-05','https://investor.marvell.com/news-events/press-releases/detail/1011/marvell-technology-inc-reports-fourth-quarter-and-fiscal-year-2026-financial-results','FY2026 Q3 -> Q4'),
 ('BE','2025-10-28','https://investor.bloomenergy.com/press-releases/press-release-details/2025/Bloom-Energy-Reports-Third-Quarter-2025-Financial-Results/default.aspx',
  '2026-02-05','https://investor.bloomenergy.com/press-releases/press-release-details/2026/Bloom-Energy-Reports-Fourth-Quarter-and-Full-Year-2025-Financial-Results-with-Record-Full-Year-Revenues/','2025 Q3 -> Q4'),
 ('STX','2025-10-28','https://investors.seagate.com/news/news-details/2025/Seagate-Technology-Reports-Fiscal-First-Quarter-2026-Financial-Results/',
  '2026-01-27','https://investors.seagate.com/news/news-details/2026/Seagate-Technology-Reports-Fiscal-Second-Quarter-2026-Financial-Results/','FY2026 Q1 -> Q2'),
 ('CRCL','2025-11-12','https://www.circle.com/pressroom/circle-reports-third-quarter-2025-results',
  '2026-02-25','https://www.circle.com/pressroom/circle-reports-fourth-quarter-and-full-fiscal-year-2025-financial-results','2025 Q3 -> Q4'),
 ('NG','2025-10-01','https://novagold.com/novagold-files-third-quarter-2025-report/',
  '2026-01-22','https://www.sec.gov/Archives/edgar/data/1173420/000117184326000375/f8k_012226.htm','FY2025 Q3 -> Q4'),
 ('MRNA','2025-11-06','https://www.sec.gov/Archives/edgar/data/1682852/000168285225000073/mrna-20251106.htm',
  '2026-02-13','https://www.sec.gov/Archives/edgar/data/1682852/000168285226000015/mrna-20260213.htm','2025 Q3 -> Q4; January12 preliminary financial update separately unresolved'),
 ('FCEL','2025-12-18','https://investor.fce.com/press-releases/press-release-details/2025/FuelCell-Energy-Ends-FY2025-with-Revenue-Growth-and-a-Focus-on-Data-Center-Opportunities/default.aspx',
  '2026-03-09','https://investor.fce.com/press-releases/press-release-details/2026/FuelCell-Energy-Delivers-Strong-Q126-Revenue-Growth-vs-Q125-Advances-Data-Center-Power-Strategy/default.aspx','FY2025 Q4 -> FY2026 Q1'),
 ('GLW','2025-10-28','https://www.corning.com/worldwide/en/about-us/news-events/news-releases/2025/10/corning-reports-third-quarter-2025-financial-results.html',
  '2026-01-28','https://www.corning.com/worldwide/en/about-us/news-events/news-releases/2026/01/corning-reports-fourth-quarter-and-full-year-2025-financial-results.html','2025 Q3 -> Q4'),
 ('RIVN','2025-11-04','https://www.sec.gov/Archives/edgar/data/1874178/000187417825000051/rivn-20251104.htm',
  '2026-02-12','https://www.sec.gov/Archives/edgar/data/1874178/000187417826000007/rivn-20260212.htm','2025 Q3 -> Q4'),
 ('PLTR','2025-11-03','https://investors.palantir.com/events.html',
  '2026-02-02','https://investors.palantir.com/events.html','2025 Q3 -> Q4; issuer index links corresponding earnings releases'),
 ('CLSK','2025-11-25','https://investors.cleanspark.com/news/news-details/2025/CleanSpark-Reports-Transformative-FY-2025-Results/default.aspx',
  '2026-02-05','https://www.sec.gov/Archives/edgar/data/827876/000119312526039382/clsk-ex99_1.htm','FY2025 Q4 -> FY2026 Q1'),
 ('MSTR','2025-10-30','https://www.strategy.com/press/strategy-announces-third-quarter-2025-financial-results_10-30-2025',
  '2026-02-05','https://www.strategy.com/press/strategy-announces-fourth-quarter-2025-financial-results_02-05-2026','2025 Q3 -> Q4'),
 ('ORCL','2025-12-10','https://investor.oracle.com/investor-news/news-details/2025/Oracle-Announces-Fiscal-Year-2026-Second-Quarter-Financial-Results/',
  '2026-03-10','https://www.oracle.com/news/announcement/q3fy26-earnings-release-2026-03-10/','FY2026 Q2 -> Q3'),
 ('RXT','2025-11-06','https://ir.rackspace.com/news-releases/news-release-details/rackspace-technology-reports-third-quarter-2025-results/',
  '2026-02-26','https://ir.rackspace.com/news-releases/news-release-details/rackspace-technology-reports-fourth-quarter-and-full-year-2025','2025 Q3 -> Q4'),
]

PLANS = [
 ('STX','2026-01-13','2026-01-27','https://investors.seagate.com/news/news-details/2026/Seagate-Technology-to-Report-Fiscal-Second-Quarter-2026-Financial-Results-on-January-27-2026/default.aspx'),
 ('CRCL','2026-01-21','2026-02-25','https://www.circle.com/pressroom/circle-to-announce-q4-and-full-fiscal-year-2025-financial-results-on-february-25-2026'),
 ('MSTR','2026-01-15','2026-02-05','https://www.strategy.com/press/strategy-announces-earnings-release-date-and-live-video-webinar-for-fourth-quarter-2025-financial-results_01-15-2026'),
]


def collect():
    folder=ROOT/'earnings'
    private=folder/'private_source_pages'; private.mkdir(parents=True,exist_ok=True)
    urls=sorted({r[2] for r in PAIRS}|{r[4] for r in PAIRS}|{r[3] for r in PLANS})
    receipts=[]
    for url in urls:
        stem=hashlib.sha256(url.encode()).hexdigest()
        receipt_path=private/f'{stem}.json'
        if receipt_path.exists():
            receipts.append(read(receipt_path)); continue
        started=utc()
        try:
            response=requests.get(url,timeout=25,headers={'User-Agent':'BenAITradingResearch/1.1 (personal financial research)'})
            raw=response.content
            path=private/f'{stem}.html';path.write_bytes(raw)
            receipt={'url':url,'http_status':response.status_code,'request_started_at':started,'source_received_at':utc(),
                     'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),
                     'status':'ACCESS_OK' if response.status_code==200 else 'HTTP_ERROR',
                     'historical_network_received_at':'UNKNOWN'}
        except requests.RequestException as exc:
            receipt={'url':url,'status':'REQUEST_FAILED','error_class':type(exc).__name__,
                     'request_started_at':started,'source_received_at':None,'historical_network_received_at':'UNKNOWN'}
        write(receipt_path,receipt);receipts.append(receipt)
    facts=[]
    for symbol,prior,prior_url,nxt,nxt_url,sequence in PAIRS:
        # MRNA's January12 preliminary financial release is a specific unresolved
        # intervening boundary; do not treat a quarterly pair as complete there.
        complete=symbol!='MRNA'
        facts.append({'symbol':symbol,'previous_release_date_ny':prior,'previous_source':prior_url,
                      'next_release_date_ny':nxt,'next_source':nxt_url,'fiscal_sequence':sequence,
                      'tier':'B_RETROSPECTIVE_EARNINGS_EXCLUSION' if complete else 'C_INTERVENING_EVENT_REVIEW_REQUIRED',
                      'coverage_complete':complete,'coverage_start':'2026-01-02','coverage_end':nxt,
                      'actual_release_time':'UNKNOWN','planned_publication_time':'UNKNOWN',
                      'historical_network_received_at':'UNKNOWN','reviewed_at':utc(),
                      'evidence_basis':'Manually read issuer release text or issuer SEC Item2.02 expressly stating release date; not filing-date inference',
                      'completeness_basis':'Consecutive ordinary fiscal releases bounding January engineering entries; not an exhaustive ad-hoc announcement audit or full-period calendar',
                      'limitations':'STX IR date is NY January27, Singapore exhibit dateline January28; no timestamp invented' if symbol=='STX' else
                                    'January12 preliminary full-year revenue disclosed; event treatment unresolved and entry blocked' if symbol=='MRNA' else
                                    'Current retrieved historical sources; no prior public plan vintage established for January sample'})
    facts.append({'symbol':'BMNR','tier':'C_UNKNOWN','coverage_complete':False,'coverage_start':None,'coverage_end':None,
                  'reason':'January13 10-Q and January12 treasury operations update do not establish complete earnings release dates; no inferred date from filing',
                  'sources':['https://www.sec.gov/Archives/edgar/data/1829311/000149315226002084/form10-q.htm',
                             'https://www.sec.gov/Archives/edgar/data/1829311/000149315226001237/ex99-1.htm']})
    plans=[{'symbol':s,'publication_date':d,'conservative_known_at_ny':str(__import__('datetime').date.fromisoformat(d)+__import__('datetime').timedelta(days=1))+'T00:00:00',
            'publication_time':'UNKNOWN','availability_basis':'NEXT_DATE_BOUND_NOT_FABRICATED_PUBLICATION_TIMESTAMP',
            'planned_release_date':release,'source':url} for s,d,release,url in PLANS]
    result={'version':'B11_PRIMARY_EARNINGS_V1','created_at':utc(),'facts':facts,'plans':plans,
            'source_receipts':receipts,'scope':'Frozen first20 engineering sample only',
            'important':'TierB uses future actual dates deliberately; never strict PIT. Successful web receipt does not itself verify content; facts were separately reviewed.'}
    write(folder/'PRIMARY_EARNINGS.json',result)
    return result


if __name__=='__main__':
    r=collect();print({'facts':len(r['facts']),'source_receipts':len(r['source_receipts'])})
