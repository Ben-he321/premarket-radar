"""Streaming archive verification; unchanged B1.2 trading and block format."""
import hashlib
import json
import time
from src.ben_b1.replay import _hash, _json
from src.ben_b1_2.shared import SharedReplayEngine

def mapping_digest(rows,guard=None):
    """Hash sorted unique (str key, canonical JSON value) rows like old dict SHA.

    SQLite BINARY ordering of the UTF-8 event identifiers equals Python's
    Unicode code point order. Enforce that order, never silently reorder rows.
    Each caller still verifies the value's own digest when one exists.
    """
    h=hashlib.sha256();h.update(b'{');previous=None;count=0
    for key,value in rows:
        if count%8192==0 and guard:guard()
        if not isinstance(key,str) or (previous is not None and key<=previous):
            raise ValueError('ARCHIVE_KEYS_NOT_STRICTLY_SORTED_UNIQUE')
        if count:h.update(b',')
        h.update(json.dumps(key,ensure_ascii=False,separators=(',',':')).encode('utf-8'))
        h.update(b':');h.update(_json(value).encode('utf-8'))
        previous=key;count+=1
    h.update(b'}');return count,h.hexdigest()

class StreamingSharedEngine(SharedReplayEngine):
    # Reporting hook is process-local and never enters the financial payload.
    verification_hook=None

    def _verify_archive(self):
        self._db.execute('PRAGMA cache_size=-131072')
        self._db.execute('PRAGMA temp_store=FILE')
        started=time.monotonic();last=0.;failure=[]
        def progress():
            nonlocal last
            if time.monotonic()-last<10:return 0
            last=time.monotonic()
            if self.verification_hook:
                try:self.verification_hook({'phase':'SQLITE_FULL_INTEGRITY_CHECK','elapsed_seconds':last-started})
                except Exception as exc:failure.append(exc);return 1
            return 0
        self._db.set_progress_handler(progress,100000)
        try:
            rows=self._db.execute('PRAGMA integrity_check').fetchall()
            if rows!=[('ok',)]:raise ValueError('ARCHIVE_INTEGRITY_FAILURE')
        except Exception:
            if failure:raise failure[0]
            raise
        finally:self._db.set_progress_handler(None,0)
        previous=None;total_events=total_quotes=0
        for sequence in range(1,self.state['archive']['applied_sequence']+1):
            if self.verification_hook:self.verification_hook({'phase':'STREAM_CANONICAL_BLOCK_VERIFY','sequence':sequence,'elapsed_seconds':time.monotonic()-started})
            row=self._db.execute('SELECT block_hash,payload FROM archive_blocks WHERE sequence=?',(sequence,)).fetchone()
            if row is None:raise ValueError('ARCHIVE_GENERATION_MISSING')
            block=json.loads(row[1])
            if _hash(block)!=row[0] or block['previous_hash']!=previous:raise ValueError('ARCHIVE_HASH_CHAIN_MISMATCH')
            def verify_guard():
                nonlocal last
                if time.monotonic()-last>=10:
                    last=time.monotonic()
                    if self.verification_hook:self.verification_hook({'phase':'STREAM_CANONICAL_BLOCK_VERIFY','sequence':sequence,'elapsed_seconds':last-started})
            event_rows=self._db.execute('SELECT event_id,digest FROM archived_events WHERE block_sequence=? ORDER BY event_id COLLATE BINARY',(sequence,))
            event_count,event_digest=mapping_digest(event_rows,verify_guard)
            if event_count!=block['event_count'] or event_digest!=block['event_digest']:raise ValueError('ARCHIVED_EVENT_DIGEST_MISMATCH')
            def quote_rows():
                cursor=self._db.execute('SELECT inventory_id,payload,digest FROM archived_quotes WHERE block_sequence=? ORDER BY inventory_id COLLATE BINARY',(sequence,))
                for key,payload,expected in cursor:
                    value=json.loads(payload)
                    if _hash(value)!=expected:raise ValueError('ARCHIVED_QUOTE_DIGEST_MISMATCH')
                    yield key,value
            quote_count,quote_digest=mapping_digest(quote_rows(),verify_guard)
            if quote_count!=block['zero_consumption_inventory_count'] or quote_digest!=block['inventory_digest']:raise ValueError('ARCHIVED_QUOTE_DIGEST_MISMATCH')
            total_events+=event_count;total_quotes+=quote_count;previous=row[0]
        archive=self.state['archive']
        if previous!=archive['applied_hash'] or total_events!=archive['archived_events'] or total_quotes!=archive['archived_zero_consumption_inventories']:
            raise ValueError('ARCHIVE_CHECKPOINT_GENERATION_MISMATCH')
        if self.verification_hook:self.verification_hook({'phase':'ARCHIVE_ALL_CHECKS_PASS','event_count':total_events,'quote_count':total_quotes,'elapsed_seconds':time.monotonic()-started})
