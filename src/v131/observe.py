"""Read-only frozen-forward reporting. No market client or execution imports.

SQLite connections are mode=ro + query_only. Only forward_observation is written.
Source timestamps are never replaced by observation time. Sampling is not a full
intraday equity history; a cycle needs actual linked events and holding evidence.
"""
import hashlib,json,sqlite3,subprocess,zipfile
from datetime import datetime,timezone,date,time,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from filelock import FileLock
from src.data.alpaca_calendar import sessions
from .runtime import root,read,write,sha,digest,utc

BOOKS=('EXP_M20_H20','CONTROL_U_H20')
NY=ZoneInfo('America/New_York');MADRID=ZoneInfo('Europe/Madrid')

def read_book(path):
    if not path.exists():return None
    with sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=10) as c:
        c.execute('PRAGMA query_only=ON');c.execute('BEGIN')
        state=json.loads(c.execute('SELECT payload FROM state WHERE id=1').fetchone()[0])
        return {'ledger':state,**{table:[json.loads(r[0]) for r in c.execute(f'SELECT payload FROM {table} ORDER BY id')] for table in ['intents','events','orders']}}

def decision_time(now):
    local=now.astimezone(NY)
    day=next(d for d in sessions(local.date(),local.date()+timedelta(days=14)) if datetime.combine(d,time(6,30),NY)>now)
    stamp=datetime.combine(day,time(6,30),NY)
    return {'new_york':stamp.isoformat(),'madrid':stamp.astimezone(MADRID).isoformat(),'calendar':'NYSE / pandas_market_calendars','timezones':['America/New_York','Europe/Madrid']}

def metrics(book):
    if book is None:return {'status':'LEDGER_MISSING'}
    l=book['ledger'];positions=l['positions'];events=book['events']
    unsettled=sum(x['amount'] for x in l['unsettled']);dividends=sum(x['amount'] for x in l['dividends'])
    held=sum(p['qty']*p['last'] for p in positions.values())
    reserved=sum(p['exit_fee_reserve'] for p in positions.values())+sum(l['pending'].values())+sum(max(0.,-x['amount']) for x in l['unsettled'])
    # Closed trade friction includes both sides; open positions add entry once.
    friction=sum(t['friction_usd'] for t in l['trades'])+sum(p['entry_friction'] for p in positions.values())
    equity=l['cash']+unsettled+dividends+held
    return {'status':'READ_ONLY_OBSERVED','cash':l['cash'],'reserved_cash':round(reserved,2),'available_cash':round(l['cash']-reserved,2),
            'positions':positions,'holding_value':round(held,2),'unsettled':round(unsettled,2),'dividend_receivable':round(dividends,2),
            'equity':round(equity,2),'commissions':l['fees'],'friction_usd':round(friction,2),'cost_total':round(l['fees']+friction,2),
            'net_pnl':round(equity-5500,2),'realized_pnl':l['realized'],'buy_fills':sum(e['type']=='BUY' for e in events),
            'completed_trades':sum(e['type']=='SELL' for e in events),'immutable_intents':len(book['intents']),
            'mark_basis':'Existing ledger last marks; may be stale; no new price request; net PnL already includes costs'}

def archive_decisions(base,dest,books):
    journal=dest/'decisions';journal.mkdir(exist_ok=True);result=[]
    for path in sorted((base/'forward/decisions').glob('*.json')):
        progress=read(path);day=path.stem;matched={name:[x for x in b['intents'] if x['side']=='BUY' and x['planned_execution_time'][:10]==day] for name,b in books.items() if b}
        dates={x['signal_asof'] for rows in matched.values() for x in rows}
        dates|={x['signal_date'] for x in progress.get('coverage',[]) if x.get('signal_date')}
        input_files=[base/'forward_cache'/d/'decision_inputs.json' for d in sorted(dates)]
        evidence=[];sources=[]
        for f in input_files:
            values=read(f,{})
            if not values:continue
            h=sha(f);sources.append({'file':str(f.relative_to(base)),'observed_input_version':h,'cutoff':values.get('cutoff','UNKNOWN'),
                'pipeline_assembled_at':values.get('received_at','UNKNOWN'),'market_received_at':'UNKNOWN',
                'receipt_limit':'Existing received_at is generated after input assembly; original HTTP/cache receipt time is not recorded',
                'version_limit':'Hash of persisted input observed by reporter, not proof of original per-request raw cache vintage'})
            frozen=journal/f'input-{h}.json'
            if not frozen.exists():write(frozen,{'source_sha256':h,'original_record':values,'first_observed_at':utc()})
        by_symbol={x['symbol']:x for f in input_files for x in read(f,{}).get('candidates',[])}
        for name,book in books.items():
            if not book:continue
            for row in progress.get('coverage',[]):
                s=row['symbol'];intent=next((x for x in matched[name] if x['symbol']==s),None)
                skips=[e for e in book['events'] if e['type']=='DECISION_SKIP' and e.get('symbol')==s and e.get('created_at','')[:10]==day]
                signal=by_symbol.get(s,{}).get('M20','UNKNOWN') if name=='EXP_M20_H20' else row.get('state')=='QUALIFIED'
                reason='INTENT_CREATED' if intent else skips[-1]['reason'] if skips else 'RECORDED_ACTION_BLOCK' if s in progress.get('blocked_symbols',[]) else row['state'] if row['state']!='QUALIFIED' else 'RECORDED_M20_SIGNAL_FALSE' if signal is False else 'UNKNOWN_NO_EXPLICIT_SKIP_RECORD'
                evidence.append({'book':name,'symbol':s,'signal':signal,'signal_hash':(intent or by_symbol.get(s,{})).get('signal_hash','UNKNOWN'),
                    'reason':reason,'intent_created_at':intent['created_at'] if intent else None,'intent_id':intent['id'] if intent else None,
                    'cutoff':intent.get('known_data_cutoff','UNKNOWN') if intent else next((x['cutoff'] for x in sources),'UNKNOWN'),
                    'recorded_input_assembled_at':intent.get('received_at','UNKNOWN') if intent else next((x['pipeline_assembled_at'] for x in sources),'UNKNOWN'),
                    'market_received_at':'UNKNOWN','strategy_version':intent.get('strategy_version','UNKNOWN') if intent else 'NO_INTENT'})
        payload={'session_day':day,'original_decision':progress,'input_sources':sources,'rows':evidence,'original_intents':matched,
                 'coverage_limit':'A read-only observer cannot recover overwritten intermediate retries or unrecorded HTTP receipt timestamps'}
        version=digest(payload);target=journal/f'{day}-{version}.json'
        if not target.exists():write(target,{**payload,'first_observed_at':utc()})
        result.append({'day':day,'version':version,'report':str(target.relative_to(dest))})
    return result

def complete_cycle(book,history,name):
    if not book:return None
    intents={x['id']:x for x in book['intents']};events=book['events'];l=book['ledger']
    buys={e['intent_id']:e for e in events if e['type']=='BUY'}
    for sell in sorted((e for e in events if e['type']=='SELL'),key=lambda x:x['booked_at']):
        exit_intent=intents.get(sell['intent_id'],{});parent=exit_intent.get('parent_id');buy=buys.get(parent);intent=intents.get(parent,{})
        if not buy or not intent or datetime.fromisoformat(intent['created_at'])>=datetime.fromisoformat(buy['effective_at']):continue
        hold=next((snap for snap in history if datetime.fromisoformat(buy['booked_at'])<=datetime.fromisoformat(snap['observed_at'])<=datetime.fromisoformat(sell['booked_at']) and
                   any(p.get('paper_intent')==parent for p in snap['books'].get(name,{}).get('ledger',{}).get('positions',{}).values())),None)
        if hold is None:continue
        exitday=date.fromisoformat(sell['exit_date']);due=next(d for d in sessions(exitday+timedelta(days=1),exitday+timedelta(days=14)))
        due_index=len(sessions(date(2016,1,1),due))-1
        settlement=next((e for e in sorted(events,key=lambda x:x.get('booked_at',x.get('created_at',''))) if e['type']=='CASH_SETTLEMENT' and datetime.fromisoformat(e['booked_at'])>=datetime.fromisoformat(sell['booked_at']) and datetime.fromisoformat(e['booked_at']).astimezone(NY).date()>=due),None)
        # Require an actual observed unsettled item, then its disappearance.
        obligation=next((snap for snap in reversed(history) if datetime.fromisoformat(sell['booked_at'])<=datetime.fromisoformat(snap['observed_at']) and settlement and datetime.fromisoformat(snap['observed_at'])<datetime.fromisoformat(settlement['booked_at']) and
                         any(x['due']==due_index and abs(x['amount']-sell['exit_proceeds'])<.005 for x in snap['books'].get(name,{}).get('ledger',{}).get('unsettled',[]))),None)
        if not settlement or obligation is None:continue
        if any(x['due']==due_index for x in l['unsettled']):continue
        before=obligation['books'][name]['ledger'];settlement_day=datetime.fromisoformat(settlement['booked_at']).astimezone(NY).date()
        actual_index=len(sessions(date(2016,1,1),settlement_day))-1
        expected=sum(x['amount'] for x in before['unsettled'] if x['due']<=actual_index)+sum(x['amount'] for x in before['dividends'] if x.get('pay_date') and x['pay_date']<=str(settlement_day))
        if abs(expected-settlement['amount'])>.011:continue
        return {'book':name,'buy_intent':intent,'exit_intent':exit_intent,'buy_event':buy,'holding_observed_at':hold['observed_at'],
                'holding_state':hold['books'][name]['ledger']['positions'],'sell_event':sell,'unsettled_observed_at':obligation['observed_at'],
                'unsettled_before':obligation['books'][name]['ledger']['unsettled'],'settlement_event':settlement,'due_session':str(due),
                'settlement_basis':'Recorded aggregate cash-settlement amount reconciled to observed due liabilities/dividends and obligation disappearance; engine has no per-trade settlement ID',
                'forward_type':'NATURAL_DELAYED_SIP_PAPER_PROXY_NOT_BROKER_FILL'}
    return None

def export_cycles(dest,books,history):
    records={}
    for name,book in books.items():
        target=dest/f'first_natural_cycle_{name}.zip'
        if target.exists():records[name]={'status':'EXPORTED','path':str(target),'sha256':sha(target)};continue
        cycle=complete_cycle(book,history,name)
        if cycle is None:
            records[name]={'status':'WAITING_NATURAL_COMPLETE_EVIDENCE','buy_fills':metrics(book).get('buy_fills'),
                           'completed_trades':metrics(book).get('completed_trades'),'requires':'Linked buy, held snapshot, sell, observed unsettled liability and actual cash settlement; no backfill'};continue
        payload=json.dumps(redacted(cycle),ensure_ascii=False,indent=2).encode()
        with zipfile.ZipFile(target,'x',zipfile.ZIP_DEFLATED) as z:
            z.writestr('cycle_events.json',payload);z.writestr('manifest.json',json.dumps({'sha256':hashlib.sha256(payload).hexdigest(),'created_at':utc(),'excluded':'Credentials, database, full market cache'}))
        records[name]={'status':'EXPORTED','path':str(target),'sha256':sha(target)}
    write(dest/'cycle_status.json',records);return records

def redacted(value):
    if isinstance(value,dict):
        return {k:('[REDACTED]' if any(term in k.lower() for term in ['api_key','secret','password','authorization','token']) else redacted(v)) for k,v in value.items()}
    if isinstance(value,list):return [redacted(x) for x in value]
    return value

def run(base=None,now=None):
    base=Path(base or root());now=now or datetime.now(timezone.utc);dest=base/'forward_observation';dest.mkdir(exist_ok=True)
    with FileLock(dest/'report.lock',timeout=0):
        status=read(base/'forward_status.json',{});protocol=read(base/'FORWARD_PROTOCOL.json',{})
        books={name:read_book(base/'forward'/name/'ledger.sqlite') for name in BOOKS}
        snapshot={'books':books,'protocol_hash':protocol.get('protocol_hash')};version=digest(snapshot)
        snapshots=dest/'snapshots';snapshots.mkdir(exist_ok=True)
        snapfile=snapshots/f'{version}.json'
        if not snapfile.exists():write(snapfile,{**snapshot,'observed_at':now.isoformat()})
        history=sorted((read(p) for p in snapshots.glob('*.json')),key=lambda x:x['observed_at'])
        rows={name:metrics(book) for name,book in books.items()}
        for name,row in rows.items():
            values=[metrics(s['books'].get(name)).get('equity') for s in history];values=[x for x in values if x is not None]
            peak=5500.;dd=0.
            for value in values:peak=max(peak,value);dd=min(dd,value/peak-1)
            row.update(observed_max_drawdown=dd,drawdown_basis='Observed snapshots only; unsampled intraday drawdown UNKNOWN',observation_count=len(values))
        diff={k:round(rows[BOOKS[0]][k]-rows[BOOKS[1]][k],6) for k in ['cash','reserved_cash','holding_value','equity','cost_total','net_pnl','completed_trades','observed_max_drawdown'] if k in rows[BOOKS[0]] and k in rows[BOOKS[1]]}
        heartbeat=status.get('heartbeat');age=(now-datetime.fromisoformat(heartbeat)).total_seconds() if heartbeat else None
        result={'checked_at':now.isoformat(),'manager_pid':status.get('pid'),'service':status.get('service','UNKNOWN'),'heartbeat':heartbeat,
                'heartbeat_age_seconds':age,'heartbeat_fresh':age is not None and 0<=age<90,'next_decision':decision_time(now),
                'service_reported_next_decision':status.get('next_decision'),'books':rows,'momentum_minus_control':diff,
                'protocol_hash':protocol.get('protocol_hash'),'errors':status.get('errors',[]),'sampling_started_at':history[0]['observed_at'],
                'decision_archive':archive_decisions(base,dest,books),'cycle_evidence':export_cycles(dest,books,history),
                'constraints':'Read-only ledger access; no execution calls, market requests, historical replay or configuration writes',
                'receipt_limit':'Historical cache HTTP received times UNKNOWN. Recorded input assembly times are preserved, never presented as original receipt times.'}
        write(dest/'latest.json',result)
        local=now.astimezone(NY);week=f'{local.isocalendar().year}-W{local.isocalendar().week:02d}'
        for folder,key in [('daily',str(local.date())),('weekly',week)]:
            target=dest/folder/f'{key}.json'
            # Current period summary, with append-only raw observations retained.
            write(target,{'period':key,'basis':'Latest available read-only snapshot; incomplete periods not projected','asof':result})
        def money(x):return f'{x:.2f}'
        lines=['# 冻结前向只读运行核对',f'检查：{now.isoformat()}。管理进程 {status.get("pid")}；心跳 {heartbeat}。',
               f'下次决策：纽约 {result["next_decision"]["new_york"]}；马德里 {result["next_decision"]["madrid"]}。',
               '| 账本 | 现金 | 预留 | 持仓市值 | 成本 | 净收益 | 已平仓交易 | 采样回撤 |','| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
        for name,r in rows.items():
            if r['status']=='LEDGER_MISSING':lines.append(f'| {name} | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |');continue
            lines.append(f'| {name} | {money(r["cash"])} | {money(r["reserved_cash"])} | {money(r["holding_value"])} | {money(r["cost_total"])} | {money(r["net_pnl"])} | {r["completed_trades"]} | {r["observed_max_drawdown"]:.2%} |')
        lines += [f'动量减对照：净收益 {diff.get("net_pnl","UNKNOWN")} USD；成本 {diff.get("cost_total","UNKNOWN")} USD。',
                  '预留不是新增资产；净收益已包含账本费用，成本仅另列说明，不重复扣减。持仓使用账本现有最后价格，可能滞后。',
                  '回撤仅根据实际保存的观察快照，未采样时段为 UNKNOWN；没有重算行情或回测。日报/周报只汇总截至当前的记录，不预测完整周期。',
                  '输入接收时间：已有 received_at 属于输入整理记录；原行情接收时间缺失时为 UNKNOWN。没有用文件修改时间或本次观察时间补造。',
                  '决策归档来自现有缓存、决策日志和不可变意图。输入文件保存观察时哈希；若原调用没有记录精确原始数据版本，不声称可以事后证明。',
                  f'自然周期：{json.dumps(result["cycle_evidence"],ensure_ascii=False)}。只有完整可连接的实际事件出现后才导出 ZIP。',
                  '本报告不启动、停止或重置交易服务。策略与统计规则继续冻结。']
        (dest/'RUNTIME_CHECK.md').write_text('\n\n'.join(lines),encoding='utf-8')
        return result

if __name__=='__main__':
    x=run();print(json.dumps({'checked_at':x['checked_at'],'pid':x['manager_pid'],'fresh':x['heartbeat_fresh'],'next_decision':x['next_decision'],'books':{k:{n:v[n] for n in ['cash','reserved_cash','net_pnl','completed_trades'] if n in v} for k,v in x['books'].items()},'cycle_evidence':x['cycle_evidence']},ensure_ascii=False))
