"""Disk ordering replaces whole-day lists; logical run/commit boundaries stay."""
import hashlib
import json
from pathlib import Path
import sqlite3
import zlib
import uuid
import copy
import time
from src.ben_b1 import rules
from src.ben_b1.replay import PRIORITY, _json
from src.ben_b1.replay import _hash
import pandas as pd
from .engine import StreamingSharedEngine, mapping_digest

class EventSpool:
    def __init__(self,directory,guard=None):
        self.directory=Path(directory).resolve();self.directory.mkdir(parents=True,exist_ok=True)
        self.path=self.directory/(uuid.uuid4().hex+'.sqlite')
        self.db=sqlite3.connect(self.path);self.guard=guard;self.count=0;self.sealed=False
        self.db.execute('PRAGMA journal_mode=DELETE')
        self.db.execute('PRAGMA temp_store=FILE')
        self.db.execute('PRAGMA cache_size=-32768')
        self.db.execute('CREATE TABLE events(at INTEGER,priority INTEGER,event_id TEXT,ordinal INTEGER,payload BLOB)')
        self.input_digest=hashlib.sha256()
        self._sql_checked=0.;self.sql_failure=None
        self.profile_hook=None
        self.db.set_progress_handler(self._sql_guard,100000)

    def _sql_guard(self):
        if self.guard and time.monotonic()-self._sql_checked>2:
            self._sql_checked=time.monotonic()
            try:self.guard()
            except Exception as exc:self.sql_failure=exc;return 1
        return 0

    def add(self,events):
        count=self.count;digest=self.input_digest.copy()
        try:self._add(events)
        except Exception:
            # Match original list-extension semantics: a failed generator
            # contributes no partial quote prefix to the logical event block.
            self.db.set_progress_handler(None,0)
            try:
                self.db.rollback();self.db.execute('DELETE FROM events WHERE ordinal>=?',(count,));self.db.commit()
                self.count=count;self.input_digest=digest
            finally:self.db.set_progress_handler(self._sql_guard,100000)
            if self.sql_failure:raise self.sql_failure
            raise

    def append(self,event):self.add([event])

    def _add(self,events):
        if self.sealed:raise ValueError('SEALED_EVENT_SPOOL')
        rows=[]
        for event in events:
            encoded=_json(event).encode('utf-8')
            # The original Python sort was stable even for equal full keys.
            rows.append((rules.aware(event['at']).value,PRIORITY.get(event['kind'],99),str(event['event_id']),self.count,zlib.compress(encoded,1)))
            self.input_digest.update(len(encoded).to_bytes(8,'big'));self.input_digest.update(encoded)
            self.count+=1
            if len(rows)>=4096:
                self.db.executemany('INSERT INTO events VALUES(?,?,?,?,?)',rows);self.db.commit();rows.clear()
                if self.guard:self.guard()
        if rows:self.db.executemany('INSERT INTO events VALUES(?,?,?,?,?)',rows)
        self.db.commit()

    def ordered(self):
        if not self.sealed:
            if self.guard:self.guard()
            if self.profile_hook:self.profile_hook({'phase':'DISK_EXACT_SORT_START','input_rows':self.count})
            try:self.db.execute('CREATE INDEX exact_event_order ON events(at,priority,event_id COLLATE BINARY,ordinal)')
            except sqlite3.OperationalError:
                if self.sql_failure:raise self.sql_failure
                raise
            self.db.commit();self.sealed=True
            if self.profile_hook:self.profile_hook({'phase':'DISK_EXACT_SORT_COMPLETE','input_rows':self.count})
        for row in self.db.execute('SELECT payload FROM events ORDER BY at,priority,event_id COLLATE BINARY,ordinal'):
            yield json.loads(zlib.decompress(row[0]))

    def close(self,discard=False):
        self.db.close()
        if discard:
            # Only this newly-created sort file is removed. Archive/WAL and all
            # old results are outside the explicitly checked transient root.
            target=self.path.resolve()
            if target.parent!=self.directory or target.name=='replay_archive.sqlite':raise ValueError('UNSAFE_TRANSIENT_TARGET')
            target.unlink()

class BoundedReplayEngine(StreamingSharedEngine):
    guard_hook=None
    batch_hook=None

    def _rollback_state(self,kind,heavy):
        if kind!='QUOTE':return super()._rollback_state(kind,heavy)
        # These branches are read-only throughout QUOTE handling. A fresh memo
        # for this event retains aliases; all mutable/unknown branches and the
        # entire ledger still receive the original deep rollback snapshot.
        immutable={'earnings','earnings_coverage','input_versions','corporate_unit_uncertainty',
            'active_gaps','close_finalized','unit_versions','sessions_processed','coverage_limitations',
            'b12_completed_day','archive'}
        memo={id(self.state[k]):self.state[k] for k in immutable if k in self.state}
        return copy.deepcopy({k:v for k,v in self.state.items() if k not in heavy},memo)

    def _day(self,at=None):
        if at:return super()._day(at)
        key=self.state['at'];cached=getattr(self,'_one_day_cache',None)
        if cached is None or cached[0]!=key:
            cached=(key,super()._day());self._one_day_cache=cached
        return cached[1]

    def _is_rth(self):
        key=self.state['at'];cached=getattr(self,'_one_rth_cache',None)
        if cached is None or cached[0]!=key:
            cached=(key,super()._is_rth());self._one_rth_cache=cached
        return cached[1]

    def compact_now(self):
        """Same generation and digest, using file-backed sorting not copies."""
        if self.state['q1_batch']['arrivals']:return {'status':'DEFERRED_UNCLOSED_QUOTE_TIMESTAMP_BATCH'}
        if not self.state['handled']:return {'status':'NO_NEW_HANDLED_EVENTS'}
        db=self._db;db.execute('PRAGMA temp_store=FILE')
        if self.batch_hook:self.batch_hook({'phase':'COMPACTION_DISK_STAGE','live_events':len(self.state['handled']),'live_inventory':len(self.state['quote_inventory'])})
        self._compact_checked=0.
        def sql_guard():
            if self.guard_hook and time.monotonic()-self._compact_checked>2:
                self._compact_checked=time.monotonic();self.guard_hook()
            return 0
        db.set_progress_handler(sql_guard,100000)
        db.execute('CREATE TEMP TABLE IF NOT EXISTS stage_events(event_id TEXT PRIMARY KEY,digest TEXT)')
        db.execute('CREATE TEMP TABLE IF NOT EXISTS stage_quotes(inventory_id TEXT PRIMARY KEY,payload TEXT,digest TEXT)')
        db.execute('DELETE FROM stage_events');db.execute('DELETE FROM stage_quotes')
        current={q['inventory_id'] for q in self.state['quotes'].values()}
        cutoff=rules.aware(self.state['at'])-pd.Timedelta(seconds=5)
        db.executemany('INSERT INTO stage_events VALUES(?,?)',self.state['handled'].items())
        def obsolete_rows():
            for index,(key,value) in enumerate(self.state['quote_inventory'].items()):
                if index%8192==0 and self.guard_hook:self.guard_hook()
                if key not in current and not value['ask_consumed'] and not value['bid_consumed'] and rules.aware(value['timestamp'])<cutoff:
                    yield key,_json(value),_hash(value)
        db.executemany('INSERT INTO stage_quotes VALUES(?,?,?)',obsolete_rows());db.commit()
        if self.batch_hook:self.batch_hook({'phase':'COMPACTION_CANONICAL_HASH'})
        event_count,event_digest=mapping_digest(db.execute('SELECT event_id,digest FROM stage_events ORDER BY event_id COLLATE BINARY'),self.guard_hook)
        quote_count,quote_digest=mapping_digest(((key,json.loads(value)) for key,value in db.execute('SELECT inventory_id,payload FROM stage_quotes ORDER BY inventory_id COLLATE BINARY')),self.guard_hook)
        archive=self.state['archive'];sequence=archive['applied_sequence']+1
        block={'sequence':sequence,'previous_hash':archive['applied_hash'],'cutoff':self.state['at'],
            'event_count':event_count,'zero_consumption_inventory_count':quote_count,'event_digest':event_digest,'inventory_digest':quote_digest}
        block_hash=_hash(block)
        prior=db.execute('SELECT block_hash FROM archive_blocks WHERE sequence=?',(sequence,)).fetchone()
        if prior is not None and prior[0]!=block_hash:raise ValueError('ARCHIVE_CHECKPOINT_REPLAY_DIVERGENCE')
        with db:
            bad=db.execute('SELECT 1 FROM stage_events s JOIN archived_events a USING(event_id) WHERE s.digest!=a.digest OR a.block_sequence!=? LIMIT 1',(sequence,)).fetchone()
            if bad:raise ValueError('ARCHIVED_EVENT_DIGEST_OR_GENERATION_CONFLICT')
            bad=db.execute('SELECT 1 FROM stage_quotes s JOIN archived_quotes a USING(inventory_id) WHERE s.digest!=a.digest OR a.block_sequence!=? LIMIT 1',(sequence,)).fetchone()
            if bad:raise ValueError('ARCHIVED_QUOTE_DIGEST_OR_GENERATION_CONFLICT')
            db.execute('INSERT OR IGNORE INTO archived_events SELECT event_id,digest,? FROM stage_events',(sequence,))
            db.execute('INSERT OR IGNORE INTO archived_quotes SELECT inventory_id,payload,digest,? FROM stage_quotes',(sequence,))
            db.execute('INSERT OR IGNORE INTO archive_blocks VALUES(?,?,?)',(sequence,block_hash,json.dumps(block,sort_keys=True,separators=(',',':'))))
        if self.after_archive_commit_hook is not None:self.after_archive_commit_hook(block)
        if self.batch_hook:self.batch_hook({'phase':'COMPACTION_ARCHIVE_COMMITTED','event_count':event_count,'quote_count':quote_count})
        archive.update(applied_sequence=sequence,applied_hash=block_hash,cutoff=self.state['at'],
            archived_events=archive['archived_events']+event_count,
            archived_zero_consumption_inventories=archive['archived_zero_consumption_inventories']+quote_count)
        self.state['handled'].clear()
        for row in db.execute('SELECT inventory_id FROM stage_quotes'):del self.state['quote_inventory'][row[0]]
        db.execute('DROP TABLE stage_events');db.execute('DROP TABLE stage_quotes');db.commit()
        return {'status':'ARCHIVED_WITHOUT_FINANCIAL_MUTATION',**block,'block_hash':block_hash}

    def run(self,events):
        supplied=isinstance(events,EventSpool)
        spool=events if supplied else EventSpool(self.archive_path.parent/'_transient_sort',self.guard_hook)
        spool.profile_hook=self.batch_hook
        completed=False
        try:
            if not supplied:spool.add(events)
            if self.batch_hook:self.batch_hook({'phase':'EVENT_SORT_INPUT_READY','input_rows':spool.count,'spool_bytes':spool.path.stat().st_size})
            for index,event in enumerate(spool.ordered()):
                self.process(event)
                if index%8192==0:
                    if self.guard_hook:self.guard_hook()
                    if self.batch_hook:self.batch_hook({'phase':'EVENT_PROCESSING','input_index':index,'input_rows':spool.count,'live_handled':len(self.state['handled']),'live_inventory':len(self.state['quote_inventory'])})
            # Exactly once at the same original before/late logical boundary.
            # Never flush at page/chunk boundaries or trim the timestamp group.
            self.flush_quote_batch()
            if self.guard_hook:self.guard_hook()
            if self.batch_hook:self.batch_hook({'phase':'ORIGINAL_LOGICAL_BLOCK_COMPACT','live_handled':len(self.state['handled']),'live_inventory':len(self.state['quote_inventory'])})
            self.compact_now()
            if self.checkpoint_path:self.save()
            completed=True
            return self.summary()
        finally:spool.close(discard=completed)
