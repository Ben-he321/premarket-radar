"""Frozen original top ten and deduplicated held quote reviews; no price rewriting."""
import json
from collections import defaultdict
from datetime import date,timedelta,datetime,timezone
import pandas as pd
from .runtime import *
from .identity import identity_at,registry,review
from src.v13.data import load
from src.v11.data import Market,classify_fields,NY
from src.v11.kernel import fill_amounts,rounded

def freeze_samples():
    path=root()/'original_top10_frozen.csv'
    if path.exists():return pd.read_csv(path,float_precision='round_trip')
    t=pd.read_csv(prior()/'accounts/M20-H20-base-trades.csv',float_precision='round_trip')
    top=pd.concat([t.nlargest(5,'total_net_pnl').assign(sample='ORIGINAL_TOP_5_WIN'),t.nsmallest(5,'total_net_pnl').assign(sample='ORIGINAL_TOP_5_LOSS')])
    top['original_row_index']=top.index;top.to_csv(path,index=False)
    write(root()/'original_top10_freeze_receipt.json',{'at':utc(),'source_sha':sha(prior()/'accounts/M20-H20-base-trades.csv'),'sample_sha':sha(path),'selection':'Original V13 reported net PnL; never reselected after review'})
    return top

def missing_review():
    paths=[];pairs=defaultdict(list)
    for row in read(prior()/'account_summaries.json'):
        if not row['missing_held_bar_sessions']:continue
        curve=pd.read_csv(prior()/f"accounts/{row['id']}-equity.csv")
        t=pd.read_csv(prior()/f"accounts/{row['id']}-trades.csv",float_precision='round_trip')
        for day in curve.loc[curve.missing_held_bars>0,'date']:
            held=t[(t.entry_date<=day)&(t.exit_date>=day)]
            for _,tr in held.iterrows():
                r=load(tr.symbol,'raw');on=r[r.trade_date==day]
                if not len(on) or not on.execution_eligible.all():
                    item={'account_id':row['id'],'symbol':tr.symbol,'date':day,'entry_date':tr.entry_date,'exit_date':tr.exit_date}
                    paths.append(item);pairs[(tr.symbol,day)].append(row['id'])
    assert len({x['account_id'] for x in paths})==26
    pd.DataFrame(paths).to_csv(root()/'missing_held_paths.csv',index=False)
    print({'deduplicated_missing':[(s,d,len(v)) for (s,d),v in pairs.items()]},flush=True)
    rows=[];market=Market()
    for (s,day),accounts in pairs.items():
        check_deadline();dest=root()/'targeted_private'/f'{s}-{day}-raw.parquet';dest.parent.mkdir(exist_ok=True)
        request_symbol=identity_at(s,day)['symbol'];error=None
        try:
            if dest.exists():f=pd.read_parquet(dest)
            else:f=market.daily([request_symbol],date.fromisoformat(day),date.fromisoformat(day),'raw');f.to_parquet(dest,index=False)
            f=classify_fields(f) if len(f) else f
            state0='PROVIDER_NO_BAR' if f.empty else 'PROVIDER_NO_ELIGIBLE_TRADE' if not f.execution_eligible.any() else 'PROVIDER_EXECUTABLE_BAR_NOW_AVAILABLE'
        except Exception as exc:f=pd.DataFrame();state0='API_REQUEST_FAILED';error=type(exc).__name__
        q=[]
        for p in (source()/'quarantine'/s/'raw').glob('*.parquet'):
            z=pd.read_parquet(p,filters=[('trade_date','==',day)])
            if len(z):q.extend(z.to_dict('records'))
        known=None;url=None
        if s=='SMCI' and '2018-08-23'<=day<='2020-01-13':
            known='VERIFIED_NASDAQ_SUSPENSION_OTC_INTERVAL';url='https://ir.supermicro.com/news/news-details/2018/Supermicro-Announces-Suspension-of-Trading-of-Common-Stock-on-Nasdaq-and-its-Intention-to-Appeal/default.aspx'
        if s=='CLSK' and '2024-11-07'<=day<='2024-11-11':
            known='VERIFIED_TRADING_HALT';url='https://www.sec.gov/Archives/edgar/data/827876/000095017024127680/clsk-20241115.htm'
        rows.append({'symbol':s,'date':day,'affected_accounts':len(accounts),'account_ids':'|'.join(accounts),'request_symbol':request_symbol,'feed':'sip','adjustment':'raw','asof':'-','retrieved_at':utc(),'provider_result':state0,'cause':known or 'UNKNOWN_NO_INDEPENDENT_CAUSE_VERIFICATION','source':url,'error':error,'current_rows':json.dumps(f.to_dict('records'),default=str),'original_quarantine_rows':json.dumps(q,default=str),'execution_limitation':'No fabricated replacement; original unavailable quote remains nonexecutable','applied_price_change':False})
    pd.DataFrame(rows).to_csv(root()/'missing_quote_review.csv',index=False)
    write(root()/'missing_quote_receipt.json',{'original_accounts':26,'unique_symbol_dates':len(rows),'rows':rows,'price_overlay_applied':False})

def top_review():
    samples=freeze_samples();rows=[];prices=[]
    actions=read(prior()/'actions.json');days=read(prior()/'CONTROLLED_PROTOCOL.json')['days']
    for _,t in samples.iterrows():
        s=t.symbol;raw=load(s,'raw').set_index('trade_date');adj=load(s,'all').set_index('trade_date')
        splits=[{**a,'day':d} for d,aa in actions.items() for a in aa if a['symbol']==s and a['kind']=='split' and str(date.fromisoformat(t.entry_date)-timedelta(days=35))<=d<=str(date.fromisoformat(t.exit_date)+timedelta(days=35))]
        during=[a for a in splits if t.entry_date<a['day']<=t.exit_date];ratio=1.
        for a in during:ratio*=a['ratio']
        qty_entry=t.qty/ratio;entry=raw.loc[t.entry_date];last=raw.loc[t.exit_date]
        stop=float(entry.open)*.95/ratio
        exitraw=float(last.open) if t.reason=='GAP_STOP' else stop if t.reason=='STOP' else float(last.close)
        ef=fill_amounts(float(entry.open),qty_entry,'BUY');xf=fill_amounts(exitraw,t.qty,'SELL')
        selected_days=days[days.index(t.signal_date):days.index(t.exit_date)+1]
        for day in selected_days:
            for adjustment,frame in [('raw',raw),('all',adj)]:
                if day in frame.index:
                    b=frame.loc[day];prices.append({'original_row_index':int(t.original_row_index),'canonical':s,'provider_symbol':identity_at(s,day)['symbol'] if identity_at(s,day) else 'UNKNOWN','date':day,'adjustment':adjustment,**{k:b[k] for k in ('timestamp','open','high','low','close','volume','vwap','execution_eligible')}})
        rows.append({**t.to_dict(),'identity_name':registry()[s]['name'],'identity_version':registry()[s]['version'],'signal_precedes_entry':t.signal_date<t.entry_date,'signal_return20':float(adj.loc[t.signal_date,'close']/adj.loc[days[days.index(t.signal_date)-20],'close']-1),'hold_count':days.index(t.exit_date)-days.index(t.entry_date)+1,'entry_raw_open':float(entry.open),'exit_raw_basis':exitraw,'entry_volume':float(entry.volume),'exit_volume':float(last.volume),'entry_fee_reconciles':ef['fee']==t.entry_fee,'exit_fee_reconciles':xf['fee']==t.exit_fee,'entry_price_reconciles':abs(ef['fill_price']/ratio-t.entry)<.00011,'exit_price_reconciles':abs(xf['fill_price']-t.exit)<.00011,'nearby_splits':json.dumps(splits),'daily_ordering':'DAILY_OPEN_GAP_THEN_STOP_THEN_H_CLOSE; INTRADAY_FILL_UNVERIFIED','raw_all_basis':'Raw fills; all-adjusted signal series; adjustment vintage not point-in-time','share_handling':'Explicit ratio and entry cost unchanged; no invented cash-in-lieu','verification':'PINNED_REAL_DAILY_RECONCILIATION_ONLY_NOT_BROKER_OR_INTRABAR_PROOF'})
    pd.DataFrame(rows).to_csv(root()/'important_trade_review.csv',index=False)
    pd.DataFrame(prices).to_csv(root()/'important_trade_price_evidence.csv',index=False)

def run():
    freeze_samples();review();missing_review();top_review();state('TARGETED_EVIDENCE_REVIEW_COMPLETE')

if __name__=='__main__':run()
