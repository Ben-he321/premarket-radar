"""One local Windows manager, two isolated books, shared cache and rate budget."""
import os,time as clock,msvcrt
from datetime import datetime,timezone,timedelta,time
from .runtime import *
from .paper import Account,schedule,dt
from .forward_protocol import freeze,book_config,BOOKS
from .forward_data import Market,inputs,action_calendar
from .identity import registry
from src.v11.data import NY
from src.data.alpaca_calendar import sessions

def probe():
    freeze();market=Market();now=datetime.now(timezone.utc)
    values=inputs(now,market);actions=action_calendar(now,market)
    good=[r for r in values['coverage'] if r['state']=='QUALIFIED']
    result={'at':utc(),'status':'PASS' if good else 'BLOCKED_NO_QUALIFIED_REAL_INPUT','qualified':len(good),'coverage':len(values['coverage']),'signal_date':values['signal_date'],'source':'REAL_ALPACA_SIP_CACHED_PLUS_INCREMENTAL','new_intents_authored':0,'errors':values['errors']}
    write(root()/'forward_input_probe.json',result);print(result,flush=True);return result

def engineering_gate():
    failures=[]
    for name in ['engineering_checks.json','restatement_engineering.json','forward_input_probe.json']:
        if read(root()/name,{}).get('status')!='PASS':failures.append(name)
    if preservation()['status']!='PASS':failures.append('OLD_EVIDENCE_PRESERVATION')
    write(root()/'forward_engineering_gate.json',{'at':utc(),'status':'PASS' if not failures else 'BLOCKED','failures':failures})
    if failures:raise ValueError('FORWARD_ENGINEERING_GATE_BLOCKED_'+','.join(failures))

def service():
    engineering_gate();p=freeze();directory=root()/'forward';directory.mkdir(exist_ok=True)
    lock=(directory/'service.lock').open('a+b');lock.seek(0);lock.write(b'0');lock.flush();lock.seek(0)
    try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
    except OSError:return
    activation=directory/'activation.json'
    if not activation.exists():write(activation,{'actual_started_at':utc(),'protocol_hash':p['protocol_hash'],'source_hashes':{str(f.relative_to(CODE)):sha(f) for f in (CODE/'src/v131').glob('*.py')},'old_ledger':'Never imported into new books'})
    started=read(activation)['actual_started_at'];metadata={s:r['version'] for s,r in registry().items()}
    accounts={name:Account(directory/name/'ledger.sqlite',{**book_config(name),'started_at':started},metadata) for name in BOOKS};market=Market()
    while not (directory/'STOP').exists():
        now=datetime.now(timezone.utc);local=now.astimezone(NY);day=local.date();errors=[]
        progress_path=directory/'decisions'/f'{day}.json';progress=read(progress_path,{'attempts':0,'completed':False})
        in_window=bool(schedule(day)) and time(6,30)<=local.time().replace(tzinfo=None)<=time(9,25)
        allow_entry=str(day)<=p['scheduled_observation_end']
        for name,a in accounts.items():
            try:a.roll_cash(now)
            except Exception as exc:errors.append({'book':name,'phase':'SETTLEMENT','type':type(exc).__name__})
        if any(a.status()['positions'] or a.status()['orders'].get('PENDING_BUY') for a in accounts.values()):
            try:
                actions=action_calendar(now,market)
                for name,a in accounts.items():
                    try:a.match(now,market,actions)
                    except Exception as exc:errors.append({'book':name,'phase':'MATCH','type':type(exc).__name__})
            except Exception as exc:errors.append({'phase':'ACTIONS','type':type(exc).__name__})
        if in_window and allow_entry and not progress['completed'] and progress['attempts']<3 and (not progress.get('retry_at') or now>=dt(progress['retry_at'])):
            progress['attempts']+=1
            try:
                values=inputs(now,market);actions=action_calendar(now,market)
                blocked={a['symbol'] for a in actions.get(str(day),[]) if a['kind'] in ('BLOCK','split')}
                candidates=[x for x in values['candidates'] if x['symbol'] not in blocked]
                for name,a in accounts.items():
                    selected=[x for x in candidates if BOOKS[name]=='U' or x['M20']]
                    spec={'id':name,'hold':20,'stop':.05,'target':None}
                    # Fresh clock after downloads; never author late/backdated intents.
                    result=a.decide(datetime.now(timezone.utc),day,selected,spec,values['cutoff'],values['received_at'])
                    progress[name]={'result':result,'candidate_count':len(selected)}
                progress.update(completed=not values['errors'] and not read(root()/'latest_action_status.json',{}).get('failed_symbols'),blocked_symbols=sorted(blocked),coverage=values['coverage'])
            except Exception as exc:errors.append({'phase':'DECISION','type':type(exc).__name__})
            progress.update(updated_at=utc(),retry_at=(now+timedelta(minutes=1)).isoformat());write(progress_path,progress)
        nextday=next(d for d in sessions(day,day+timedelta(days=14)) if datetime.combine(d,time(6,30),NY)>now)
        reason='OBSERVATION_FINISHED_DRAINING_EXISTING_POSITIONS' if not allow_entry else 'DECISION_COMPLETE' if progress['completed'] else 'DATA_RETRY_OR_PER_SYMBOL_BLOCK' if in_window else 'WAITING_NEXT_DECISION_WINDOW'
        result={'service':'RUNNING','pid':os.getpid(),'heartbeat':utc(),'next_poll':(now+timedelta(seconds=30)).isoformat(),'next_decision':datetime.combine(nextday,time(6,30),NY).isoformat(),'reason':reason,'books':{name:a.status() for name,a in accounts.items()},'errors':errors,'protocol_hash':p['protocol_hash'],'actual_started_at':started,'stop_file':str(directory/'STOP'),'resume':'python -m src.v131 paper-service','service_task_name':'BenAITrading-EvidenceForward-V1_3_1','observation_end':p['scheduled_observation_end'],'status':'EXPERIMENTAL_UNPROVEN'}
        write(root()/'forward_status.json',result)
        if errors:write(directory/'errors'/f'{now.strftime("%Y%m%dT%H%M%S")}.json',errors)
        if not allow_entry and all(not a.status()['positions'] and not a.status()['unsettled'] and not a.status()['dividend_receivable'] and not a.status()['orders'].get('PENDING_BUY') for a in accounts.values()):break
        clock.sleep(30)
    result['service']='STOPPED';result['stopped_at']=utc();write(root()/'forward_status.json',result)
