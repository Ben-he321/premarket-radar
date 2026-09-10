"""Offline process/ledger scheduling, limiter and page regression checks."""
from datetime import datetime,timezone
import sqlite3
import importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from streamlit.testing.v1 import AppTest
from src.watchlist.runtime import root,read,write
from src.watchlist.paper import initialize,observe,next_run,service
from src.data.alpaca_config import PROJECT_ROOT


def test_paper_idempotent_concurrent_restart_and_no_fills():
    initialize()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _:observe({'historical':{'status':'ACCESS_OK'}},'2026-09-09'),range(8)))
    initialize();result=observe({},'2026-09-09')
    assert result['observations']==1 and result['cash']==5500 and result['fills']==0
    with sqlite3.connect(root()/'paper/ledger.sqlite') as db:
        assert db.execute('SELECT count(*) FROM intents').fetchone()[0]==0


def test_paper_stop_and_singleton():
    from filelock import FileLock
    p=initialize()
    with FileLock(str(p/'worker.lock')):assert service(once=True)['status']=='ALREADY_RUNNING'
    (p/'STOP').write_text('test')
    assert service(once=True)['status'].startswith('STOP_REQUESTED')


def test_paper_once_reads_real_source_marker_only_after_gates():
    assert service(once=True)['status']=='BLOCKED_REQUIRES_ENGINEERING_TESTS_AND_COMPLETED_RESEARCH'
    from src.data.alpaca_calendar import finalized_day
    from src.watchlist.runtime import utc
    write(root()/'engineering_checks.json',{'status':'PASS'})
    write(root()/'research/summary.json',{'status':'COMPLETE','champion':None})
    write(root()/'sync_last.json',{'end':str(finalized_day()),'at':utc(),'failures':[]})
    write(root()/'sip_checks.json',{'historical':{'status':'ACCESS_OK'}})
    result=service(once=True)
    assert result['status']=='WAITING_FOR_MARKET' and result['observations']==1 and result['fills']==0


def test_dst_and_weekend_schedule():
    # NY changes before Madrid: no hard-coded Europe/US difference.
    spring=next_run(datetime(2026,3,9,9,tzinfo=timezone.utc))
    winter=next_run(datetime(2026,2,9,9,tzinfo=timezone.utc))
    assert spring.hour==10 and winter.hour==11
    friday=next_run(datetime(2026,9,11,20,tzinfo=timezone.utc))
    assert friday.weekday()==0


def test_shared_sqlite_limiter_actual_method(tmp_path,monkeypatch):
    # Load an isolated module to exercise acquire, not the HTTP-test bypass.
    spec=importlib.util.spec_from_file_location('limiter_real',PROJECT_ROOT/'src/data/alpaca_rate.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    clock=[100.];sleeps=[]
    monkeypatch.setattr(mod.time,'time',lambda:clock[0])
    def sleep(n):sleeps.append(n);clock[0]+=n
    monkeypatch.setattr(mod.time,'sleep',sleep)
    a=mod.SharedRateLimiter(tmp_path/'rate.sqlite',limit=2)
    b=mod.SharedRateLimiter(tmp_path/'rate.sqlite',limit=2)
    a.acquire();b.acquire();a.acquire()
    assert len(sleeps)==1 and sleeps[0]>=60


def test_new_page_is_read_only_and_empty_state():
    page=PROJECT_ROOT/'pages/12_全池研究.py'
    app=AppTest.from_file(str(page),default_timeout=30).run()
    assert not app.exception and not app.error
    assert not (root()/'paper/ledger.sqlite').exists()
    assert len(app.tabs)==7


def test_backup_sqlite_handles_close_and_restore_is_independent():
    from src.watchlist.backup import backup_verify
    initialize();observe({'mock':True},'2023-01-03')
    result=backup_verify()
    assert result['status']=='PASS' and not result['hash_failures']
    assert Path(result['restore_directory'])!=root()
    with sqlite3.connect(Path(result['restore_directory'])/'paper/ledger.sqlite') as db:
        assert db.execute('SELECT count(*) FROM observations').fetchone()[0]==1
