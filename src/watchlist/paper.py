"""Real-time cash observer. No broker client and no fabricated forward fills."""
import os
import sqlite3
import time
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from filelock import FileLock,Timeout
from src.data.alpaca_calendar import sessions,finalized_day
from .runtime import root,read,write,utc,digest,event

NY=ZoneInfo('America/New_York')


def next_run(now):
    local=now.astimezone(NY)
    for offset in range(15):
        d=local.date()+timedelta(days=offset)
        target=datetime(d.year,d.month,d.day,6,30,tzinfo=NY)
        if target>local and sessions(d,d):return target.astimezone(timezone.utc)
    raise ValueError('NO_NEXT_SESSION')


def initialize():
    p=root()/'paper';p.mkdir(exist_ok=True)
    with sqlite3.connect(p/'ledger.sqlite') as db:
        db.execute('CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1), started_at TEXT NOT NULL, cash REAL NOT NULL CHECK(cash>=0), mode TEXT NOT NULL)')
        db.execute("INSERT OR IGNORE INTO account VALUES (1,?,5500,'CASH_OBSERVATION')",(utc(),))
        db.execute('CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY, created_at TEXT, signal_asof TEXT, source TEXT, status TEXT, payload TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, created_at TEXT, signal_asof TEXT, intended_execution_time TEXT, strategy_version TEXT, quantity INTEGER, exit_rules TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS fills(id TEXT PRIMARY KEY, intent_id TEXT, effective_at TEXT, retrieved_at TEXT, payload TEXT)')
    return p


def observe(checks,cutoff):
    import json
    p=initialize();key=digest({'kind':'CASH_OBSERVATION','cutoff':cutoff,'version':1})
    with sqlite3.connect(p/'ledger.sqlite',timeout=30) as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?)',(key,utc(),str(cutoff),'ALPACA_SIP_HISTORICAL_BASIC','NO_QUALIFIED_STRATEGY',json.dumps(checks)))
        account=db.execute('SELECT started_at,cash,mode FROM account WHERE id=1').fetchone()
        observations=db.execute('SELECT count(*) FROM observations').fetchone()[0]
        fills=db.execute('SELECT count(*) FROM fills').fetchone()[0]
    return {'started_at':account[0],'cash':account[1],'mode':account[2],'observations':observations,'fills':fills}


def service(days=30,once=False):
    p=initialize()
    try:
        lock=FileLock(str(p/'worker.lock'),timeout=0);lock.acquire()
    except Timeout:return {'status':'ALREADY_RUNNING'}
    try:
        if (p/'STOP').exists():return {'status':'STOP_REQUESTED_REMOVE_STOP_TO_RESUME'}
        if read(root()/'engineering_checks.json',{}).get('status')!='PASS' or read(root()/'research/summary.json',{}).get('status')!='COMPLETE':
            return {'status':'BLOCKED_REQUIRES_ENGINEERING_TESTS_AND_COMPLETED_RESEARCH'}
        end=time.time()+days*86400;scheduled=datetime.now(timezone.utc)
        while time.time()<end and not (p/'STOP').exists():
            now=datetime.now(timezone.utc)
            if now>=scheduled:
                try:
                    cutoff=str(finalized_day());last=read(p/'status.json',{})
                    latest_sync=read(root()/'sync_last.json',{})
                    recent=latest_sync.get('end')==cutoff and not latest_sync.get('failures',[1]) and latest_sync.get('at','')>(now-timedelta(minutes=30)).isoformat()
                    if not recent and (last.get('signal_asof')!=cutoff or last.get('data_status')!='ACCESS_OK'):
                        from .data import sync
                        sync(read(root()/'universe.json'),incremental=True,deadline=time.time()+3600)
                    checks=read(root()/'sip_checks.json',{})
                    data_status=checks.get('historical',{}).get('status','UNKNOWN')
                    info=observe(checks,cutoff) if data_status=='ACCESS_OK' else {'mode':'CASH_OBSERVATION','cash':5500,'fills':0}
                    scheduled=next_run(now)
                    write(p/'status.json',{**info,'status':'WAITING_FOR_MARKET','signal_asof':cutoff,'data_status':data_status,
                                           'next_run':scheduled.isoformat(),'pid':os.getpid(),'heartbeat':utc(),
                                           'service_expires_at':datetime.fromtimestamp(end,timezone.utc).isoformat(),'fees':0,'realized_pnl':0,
                                           'message':'暂无合格策略；仅更新真实行情和现金观察，不提交任何订单'})
                except Exception as exc:
                    scheduled=now+timedelta(minutes=30)
                    write(p/'status.json',{'status':'RETRY_WAIT','error_type':type(exc).__name__,'next_run':scheduled.isoformat(),'pid':os.getpid(),'heartbeat':utc()})
            if once:return read(p/'status.json')
            info=read(p/'status.json',{});info['heartbeat']=utc();write(p/'status.json',info)
            time.sleep(30)
        info=read(p/'status.json',{});info.update(status='STOPPED',heartbeat=utc());write(p/'status.json',info)
        return info
    finally:lock.release()


def launch():
    import subprocess,sys
    p=initialize()
    try:
        with FileLock(str(p/'worker.lock'),timeout=0):pass
    except Timeout:return {'status':'ALREADY_RUNNING'}
    if (p/'STOP').exists():return {'status':'STOP_REQUESTED'}
    stdout=open(p/'service.stdout.log','a',encoding='utf-8');stderr=open(p/'service.stderr.log','a',encoding='utf-8')
    from src.data.alpaca_config import PROJECT_ROOT
    child=subprocess.Popen([sys.executable,'-u','-m','src.watchlist','paper-service','--days','30'],cwd=PROJECT_ROOT,
                           stdout=stdout,stderr=stderr,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    stdout.close();stderr.close()
    return {'status':'LAUNCHED_PENDING_HEARTBEAT','launcher_pid':child.pid}
