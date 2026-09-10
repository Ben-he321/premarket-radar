"""Export immutable local artifacts and verify recovery in a new directory."""
import hashlib
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from pathlib import Path
import pandas as pd
from filelock import FileLock
from .runtime import root,read,write,utc


def backup_verify():
    source=root();destination=source.parent/'watchlist-backups';destination.mkdir(exist_ok=True)
    token=utc().replace(':','').replace('+','_')
    archive=destination/(token+'.zip');hashes={}
    with FileLock(str(source/'sync.lock'),timeout=0),zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as z, tempfile.TemporaryDirectory() as stage:
        for p in sorted(source.rglob('*')):
            if not p.is_file() or p.suffix in ('.lock','.tmp') or p.name.endswith(('-wal','-shm','-journal')):continue
            relative=p.relative_to(source).as_posix();actual=p
            if p.suffix=='.sqlite':
                actual=Path(stage)/p.name
                with closing(sqlite3.connect(p)) as a,closing(sqlite3.connect(actual)) as b:a.backup(b)
            payload=actual.read_bytes();hashes[relative]=hashlib.sha256(payload).hexdigest();z.writestr(relative,payload)
        import json
        z.writestr('BACKUP_HASHES.json',json.dumps(hashes))
    restored=Path(tempfile.mkdtemp(prefix='ben-watchlist-restore-'))
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            target=(restored/name).resolve()
            if not target.is_relative_to(restored.resolve()):raise ValueError('UNSAFE_ARCHIVE_PATH')
        z.extractall(restored)
    bad=[name for name,h in hashes.items() if hashlib.sha256((restored/name).read_bytes()).hexdigest()!=h]
    groups=0;rows=0
    for m in (restored/'datasets').rglob('manifest.json'):
        doc=read(m);frames=[pd.read_parquet(restored/c['path']) for c in doc['chunks'].values() if c['status']=='OK']
        if frames:
            f=pd.concat(frames);assert not f.duplicated(['symbol','trade_date']).any()
            groups+=1;rows+=len(f)
    result={'at':utc(),'archive':str(archive),'restore_directory':str(restored),'hashes_checked':len(hashes),
            'hash_failures':bad,'readable_groups':groups,'rows':rows,'status':'PASS' if not bad else 'FAILED',
            'original_data_overwritten':False}
    write(source/'backup_verification.json',result)
    return result
