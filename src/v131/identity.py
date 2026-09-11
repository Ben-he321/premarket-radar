"""Dated security identity, shared by price requests and action routing.

Specific reviewed facts are data, never a ticker-wide action exemption.
Unknown event types and unmatched identifiers remain blocking.
"""
from datetime import date,timedelta
from .runtime import read,write,root,prior,source,digest,utc

FACTS=[
 {'symbol':'META','kind':'name_changes','day':'2022-06-09','legal_date':'2022-06-09','old_symbol':'FB','new_symbol':'META','old_cusip':'30303M102','new_cusip':'30303M102','old_name':'Meta Platforms, Inc. Class A','new_name':'Meta Platforms, Inc. Class A','ratio':1.,'cash':0.,'treatment':'VERIFIED_SAME_SECURITY_RENAME','sources':['https://docs.alpaca.markets/us/docs/market-data-faq']},
 {'symbol':'ECHO','kind':'cash_mergers','day':'2021-11-23','legal_date':'2021-11-23','old_symbol':'ECHO','new_symbol':'DELISTED','old_cusip':'27875T101','new_cusip':'NONE','old_name':'Echo Global Logistics, Inc.','new_name':'Einstein MidCo, LLC (cash acquisition)','ratio':0.,'cash':48.25,'treatment':'NOT_APPLICABLE_OTHER_SECURITY','target_cusip':'278768106','sources':['https://www.nasdaqtrader.com/TraderNews.aspx?id=eca2021-254','https://www.globenewswire.com/news-release/2026/06/22/3315317/0/en/echostar-changing-stocker-ticker-sats-to-echo-marking-the-company-s-next-era-on-earth-and-in-space.html']},
 {'symbol':'RKLB','kind':'name_changes','day':'2025-05-27','legal_date':'2025-05-23','old_symbol':'RKLB','new_symbol':'RKLB','old_cusip':'773122106','new_cusip':'773121108','old_name':'Rocket Lab USA, Inc.','new_name':'Rocket Lab Corporation','ratio':1.,'cash':0.,'treatment':'VERIFIED_HOLDCO_ONE_FOR_ONE','sources':['https://www.nasdaqtrader.com/TraderNews.aspx?id=ECA2025-264','https://www.sec.gov/Archives/edgar/data/1819994/000162828025027473/rklb-20250523.htm']}
]

def registry():
    rows=read(prior()/'input_snapshot.json')['universe']['records'];out={}
    for r in rows:
        segments=r.get('segments') or r.get('symbol_segments') or [{'symbol':r['symbol'],'start':r['research_start'],'end':'2099-12-31'}]
        out[r['symbol']]={'name':r['name'],'status':r['status'],'version':r['identity_version'],'segments':[dict(x,cusip='UNKNOWN') for x in segments]}
    # Date boundaries below are supported by explicit reviewed sources; no IPO claims.
    out['META']['segments']=[{'symbol':'FB','start':'2016-01-01','end':'2022-06-08','cusip':'30303M102'},{'symbol':'META','start':'2022-06-09','end':'2099-12-31','cusip':'30303M102'}]
    out['ECHO']['segments']=[{'symbol':'SATS','start':'2016-01-01','end':'2026-06-23','cusip':'278768106'},{'symbol':'ECHO','start':'2026-06-24','end':'2099-12-31','cusip':'278768106'}]
    out['RKLB']['segments']=[{'symbol':'RKLB','start':'2021-08-25','end':'2025-05-26','cusip':'773122106'},{'symbol':'RKLB','start':'2025-05-27','end':'2099-12-31','cusip':'773121108'}]
    return out

def identity_at(symbol,day,book=None):
    record=(book or registry()).get(symbol,{})
    matches=[s for s in record.get('segments',[]) if s['start']<=day<=s['end']]
    return {**matches[0],'canonical':symbol,'identity_version':record['version']} if len(matches)==1 else None

def resolve_action(symbol,kind,a,book=None):
    day=a.get('ex_date') or a.get('effective_date') or a.get('process_date')
    result={'symbol':symbol,'kind':kind,'day':day,'id':a.get('id','UNKNOWN'),'raw':a,'status':'UNVERIFIED_COMPLEX_ACTION'}
    if not day:return result
    target=identity_at(symbol,day,book);before=identity_at(symbol,str(date.fromisoformat(day)-timedelta(days=1)),book)
    if not target:return result
    sid=a.get('cusip') or a.get('acquiree_cusip') or a.get('old_cusip')
    ids={x['cusip'] for x in (target,before) if x and x['cusip']!='UNKNOWN'}
    if sid and ids and sid not in ids:
        # A mismatched CUSIP can also mean an unreviewed conversion. Only an
        # independently identified different issuer supports non-applicability.
        other=next((f for f in FACTS if f['treatment']=='NOT_APPLICABLE_OTHER_SECURITY' and f['symbol']==symbol and f['day']==day and f['kind']==kind and f['old_cusip']==sid and a.get('rate')==f['cash']),None)
        return {**result,'status':'NOT_APPLICABLE_OTHER_SECURITY' if other else 'UNVERIFIED_COMPLEX_ACTION','target_identity':target}
    for f in FACTS:
        if (f['symbol'],f['kind'],f['day'])!=(symbol,kind,day):continue
        expected={'old_cusip':f['old_cusip'],'new_cusip':f['new_cusip']} if kind=='name_changes' else {'acquiree_cusip':f['old_cusip'],'rate':f['cash']}
        if all(a.get(k)==v for k,v in expected.items()):return {**result,'status':f['treatment'],'conversion_ratio':f['ratio'],'sources':f['sources'],'target_identity':target}
    if kind in ('forward_splits','reverse_splits','cash_dividends'):
        result['status']='SUPPORTED_PROVIDER_ACTION'
    elif a.get('acquirer_symbol')==target['symbol'] and a.get('acquiree_symbol')!=target['symbol']:
        result['status']='ACQUIRER_SHARES_UNCHANGED'
    return result

def review():
    import pandas as pd,json
    rows=[]
    for f in FACTS:
        raw=read(source()/'actions'/f"{f['symbol']}.json")
        a=next(a for a in raw['data'][f['kind']] if (a.get('effective_date') or a.get('process_date'))==f['day'])
        rows.append({**f,'raw_provider_fields':json.dumps(a,sort_keys=True),'provider_retrieved_at':raw['retrieved_at'],'reviewed_at':utc(),'raw_record_hash':digest(a),'resolution':resolve_action(f['symbol'],f['kind'],a)['status'],'cash_applied_to_target':0.,'held_share_multiplier':1.,'equity_treatment':'Continuous economic ownership; no invented cash','sources':' | '.join(f['sources'])})
    pd.DataFrame(rows).to_csv(root()/'corpora_action_identity_review.csv',index=False)
    write(root()/'security_identity_registry.json',registry())
    result={}
    for s in registry():
        for kind,items in read(source()/'actions'/f'{s}.json',{}).get('data',{}).items():
            if kind in ('forward_splits','reverse_splits','cash_dividends'):continue
            for a in items:
                x=resolve_action(s,kind,a)
                if x['day'] and x['day']<='2026-03-10':result.setdefault(x['day'],[]).append(x)
    write(root()/'complex_action_calendar.json',result)
    return result
