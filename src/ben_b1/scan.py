"""Read-only, hash checked full-pool daily coarse screen. No synthetic prices or fills."""
from dataclasses import asdict
import json
import hashlib
import math
from pathlib import Path
import pandas as pd
import pandas_market_calendars as mcal
from .freeze import ROOT, REPO, DATA, sha, write, now
from .rules import daily_features, initial_stop_plan, nearest_overhead, net_reward_risk


def load(record, adjustment, objects, duplicate_reviews=None):
    manifest_path = DATA / 'datasets' / record['symbol'] / record['identity_version'] / adjustment / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (manifest['symbol'],manifest['identity_version'],manifest['adjustment']) != (record['symbol'],record['identity_version'],adjustment):
        raise ValueError('CACHE_IDENTITY_MISMATCH')
    frames=[]
    for chunk in manifest['chunks'].values():
        if chunk['status'] != 'OK':
            continue
        path=DATA / chunk['path']
        actual=sha(path)
        if actual != chunk['sha256']:
            raise ValueError('SOURCE_HASH_MISMATCH')
        objects.append({'symbol':record['symbol'],'identity_version':record['identity_version'],
                        'adjustment':adjustment,'path':str(path),'sha256':actual,
                        'retrieved_at':chunk.get('retrieved_at','UNKNOWN'),
                        'source_received_at':'UNKNOWN','manifest_sha256':sha(manifest_path)})
        piece=pd.read_parquet(path)
        if len(piece) and set(piece.symbol.unique()) != {record['symbol']}:
            raise ValueError('PARQUET_SECURITY_MISMATCH')
        frames.append(piece)
    if not frames:
        return pd.DataFrame()
    f=pd.concat(frames,ignore_index=True)
    duplicated=f[f.duplicated(['symbol','trade_date'],keep=False)]
    if duplicate_reviews is not None and len(duplicated):
        for day, group in duplicated.groupby('trade_date'):
            duplicate_reviews.append({'symbol':record['symbol'],'adjustment':adjustment,'trade_date':day,
                                      'rows':len(group),'different_ohlcv':len(group[['open','high','low','close','volume']].drop_duplicates())>1,
                                      'resolution':'LATEST_CHUNK_IN_FROZEN_MANIFEST_ORDER','selected_row':group.iloc[-1].to_dict()})
    # Latest revision wins with conflicts and selected precedence explicitly recorded.
    f=f.drop_duplicates(['symbol','trade_date'],keep='last').sort_values('trade_date')
    overlay=ROOT/'data_probe'/f'latest_2026-09-11_{adjustment}.parquet'
    if overlay.exists():
        recent=pd.read_parquet(overlay)
        recent=recent[recent.symbol==record['symbol']]
        if len(recent):
            f=pd.concat([f,recent],ignore_index=True).drop_duplicates(['symbol','trade_date'],keep='last').sort_values('trade_date')
            objects.append({'symbol':record['symbol'],'identity_version':record['identity_version'],
                            'adjustment':adjustment,'path':str(overlay),'sha256':sha(overlay),
                            'source_received_at':'SEE_LATEST_OVERLAY_MANIFEST','overlay':True})
    return f[f.trade_date >= record['research_start']].set_index('trade_date')


def run():
    out=ROOT/'coarse'
    out.mkdir(parents=True,exist_ok=True)
    universe=json.loads((DATA/'universe.json').read_text(encoding='utf-8'))
    configured=json.loads((REPO/'config/watchlist.json').read_text(encoding='utf-8'))
    records=universe['records']
    assert len(records)==66 and {r['symbol'] for r in records}==set(configured['candidates'])
    objects=[]; events=[]; coverage=[]; errors=[]; duplicate_reviews=[]
    schedule=mcal.get_calendar('NYSE').schedule('2016-01-01',pd.Timestamp.now(tz='America/New_York').date())
    completed=schedule[schedule.market_close < pd.Timestamp.now(tz='UTC')]
    cutoff=str(completed.index[-1].date())
    close_by_day=dict(zip(schedule.index.strftime('%Y-%m-%d'),schedule.market_close.tolist()))
    for rec in records:
        symbol=rec['symbol']
        try:
            raw=load(rec,'raw',objects,duplicate_reviews); all_=load(rec,'all',objects,duplicate_reviews)
            common=all_.index.intersection(raw.index)
            f=all_.loc[common].loc[:cutoff].copy()
            features=daily_features(f,regular_session_verified=False)
            valid=(features[['open','high','low','close']].gt(0).all(axis=1) &
                   features.high.ge(features[['open','close','low']].max(axis=1)) &
                   features.low.le(features[['open','close','high']].min(axis=1)))
            scope= 'EXCLUDE_OIL_GAS' if symbol in ['SM','BATL'] else 'EXCLUDE_DEFENSE_PRIMARY' if symbol=='AVAV' else 'EXCLUDE_NON_OPERATING_SECURITY' if rec['type'] in ['CEF','SPAC_COMMON'] else 'PENDING_BUSINESS_REVIEW'
            symbol_events=[]
            for day,row in features[features.daily_screen_candidate & valid].iterrows():
                if day not in close_by_day:
                    continue
                scale=float(raw.loc[day,'close']/row.close)
                close=float(raw.loc[day,'close']); entry=close*1.001
                if not math.isfinite(entry) or entry<=0:
                    continue
                qty=max(0,math.floor((2750-3)/entry))
                emas={n:float(row[f'ema{n}'])*scale for n in [5,10,20,50,100]}
                stops=initial_stop_plan(close,entry,emas,qty)
                overhead=nearest_overhead(close,emas,float(row.prior_high30)*scale)
                rr=net_reward_risk(entry,stops.legs,overhead['price'],overhead_inputs_complete=overhead['status'] in {'KNOWN_OVERHEAD','NO_KNOWN_OVERHEAD_IN_DEFINED_SET'}) if stops.status=='VALID' else {'allowed':False,'status':stops.reason,'rr':None}
                e={'symbol':symbol,'trade_date':day,'identity_version':rec['identity_version'],
                   'local_security_id': 'LOCAL_'+rec['identity_version'], 'verified_global_security_id':rec.get('security_id','UNKNOWN'),
                   'identity_sort_hash':hashlib.sha256(rec['identity_version'].encode()).hexdigest(), 'identity_status':rec['status'],
                   'close_raw':close,'ema5_proxy':emas[5],'ema10_proxy':emas[10],'ema20_proxy':emas[20],
                   'ema50_proxy':emas[50],'ema100_proxy':emas[100], 'prior_high30_proxy':float(row.prior_high30)*scale,
                   'atr14_prev_proxy':float(row.atr14_prev)*scale,'valid_sessions':int(row.valid_sessions),
                   'overhead':overhead['price'],'overhead_status':overhead['status'],
                   'stop_status':stops.status,'stops':json.dumps([asdict(x) for x in stops.legs]),
                   'coarse_rr':rr.get('rr'),'coarse_space_allowed':bool(rr.get('allowed',False)),
                   'scope_preliminary':scope,'prior20_dollar_volume_proxy':float((raw.close*raw.volume).shift(1).rolling(20).mean().loc[day]),
                   'decision_time':(close_by_day[day]+pd.Timedelta(minutes=5)).isoformat(),
                   'coarse_only':True,'adjustment':'all snapshot converted to raw closing-date unit; NOT verified split-only/RTH',
                   'source_received_at':'UNKNOWN','order_created_at':None,'fill_effective_at':None,
                   'rejection_reason':'PENDING_RTH_PIT_QUOTES_ACTIONS_AND_SCOPE','input_manifest_version':sha(DATA/'datasets'/symbol/rec['identity_version']/'all/manifest.json')}
                symbol_events.append(e)
            events.extend(symbol_events)
            expected=completed[(completed.index.strftime('%Y-%m-%d')>=max(rec['research_start'],str(f.index.min())))].index.strftime('%Y-%m-%d') if len(f) else []
            coverage.append({'symbol':symbol,'rows':len(f),'first':str(f.index.min()) if len(f) else None,
                             'last':str(f.index.max()) if len(f) else None,'requested_end':cutoff,
                             'coarse_events':len(symbol_events),'coarse_space_pass':sum(e['coarse_space_allowed'] for e in symbol_events),
                             'numeric_invalid_rows':int((~valid).sum()),'missing_sessions':len(set(expected)-set(f.index)),
                             'warmup100_rows':int(features.valid_sessions.ge(100).sum()),
                             'scope_preliminary':scope,'status':'COARSE_ONLY_NOT_TRADE_QUALIFIED'})
        except Exception as exc:
            errors.append({'symbol':symbol,'error_type':type(exc).__name__,'error':str(exc)})
            coverage.append({'symbol':symbol,'status':'SOURCE_ERROR','coarse_events':None,'error':type(exc).__name__})
        write(out/'checkpoint.json',{'at':now(),'completed_symbols':len(coverage),'last_symbol':symbol,'errors':len(errors)})
    frame=pd.DataFrame(events)
    if not frame.empty:
        frame=frame.sort_values(['trade_date','identity_sort_hash']).reset_index(drop=True)
    frame.to_csv(out/'all_candidate_events.csv',index=False,encoding='utf-8-sig')
    pd.DataFrame(coverage).to_csv(out/'full_pool_coverage.csv',index=False,encoding='utf-8-sig')
    first=frame[(frame.coarse_space_allowed)&(frame.scope_preliminary=='PENDING_BUSINESS_REVIEW')&(frame.identity_status=='INCLUDED')].head(10) if not frame.empty else frame
    first.to_csv(out/'first10_probe_events.csv',index=False,encoding='utf-8-sig')
    write(out/'input_manifest.json',{'at':now(),'source_universe_sha256':sha(DATA/'universe.json'),'objects':objects,'cutoff':cutoff})
    write(out/'errors.json',errors)
    write(out/'duplicate_revision_review.json',duplicate_reviews)
    write(out/'summary.json',{'at':now(),'candidates':len(coverage),'coarse_events':len(events),'first10':first[['symbol','trade_date']].to_dict('records') if not first.empty else [],
                            'errors':len(errors),'cutoff':cutoff,'status':'COARSE_ONLY_NOT_EXECUTABLE',
                            'warning':'Daily Alpaca all is a coarse filter; neither RTH nor split-only execution acceptance. No fills or returns inferred.'})
    print(json.dumps(json.loads((out/'summary.json').read_text(encoding='utf-8'))))


if __name__=='__main__':
    run()
