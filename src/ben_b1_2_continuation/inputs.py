"""Full SIP records with page/dataset hashes, read in bounded Arrow batches."""
from pathlib import Path
import json
import math
import sqlite3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from src.ben_b1.data_probe import load_universe, digest, DataAccessError
from src.ben_b1_2.data import B12Market, query_ticker
from src.ben_b1_2.portfolio import ContinuousInputs, quote_events
from .runtime import ROOT, read, write, sha, utc, guard

def parquet_quotes(path,symbol,receipt,start,end,batch_size=4096,profile_hook=None):
    """Existing normalized IDs/receipts remain byte-for-byte field values."""
    count=0
    if path is not None:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=batch_size):
            guard();frame=batch.to_pandas()
            times=pd.to_datetime(frame.t,utc=True)
            frame=frame[times.between(pd.Timestamp(start),pd.Timestamp(end))]
            if len(frame) and 'source_received_at' not in frame:
                # Old unnormalized cache requires the exact prior receipt rule.
                # The fallback itself is bounded to this Arrow batch.
                from src.ben_b1.b11_data import attach_page_receipts
                frame=attach_page_receipts(frame,receipt)
            count+=len(frame)
            if profile_hook:profile_hook({'phase':'BOUNDED_PARQUET_READ','symbol':symbol,'read_rows':count,'batch_size':batch_size})
            yield from quote_events(symbol,frame,receipt)
    receipt['rows_in_requested_window']=count

def normalize_pages(symbol,receipt,batch_size=4096,output=None):
    """Match whole-response Pandas promotion and last-page timestamp receipts.

    Two passes use raw disk pages and a timestamp index; neither all rows nor
    an all-timestamps dictionary is held in memory. No quote is subsampled.
    """
    source=Path(receipt['path']);folder=Path(output) if output is not None else source
    folder.mkdir(parents=True,exist_ok=True)
    target=folder/'data.parquet';stage=folder/'normalization_index.sqlite'
    db=sqlite3.connect(stage);db.execute('PRAGMA cache_size=-16384');db.execute('PRAGMA temp_store=FILE')
    db.execute('CREATE TABLE IF NOT EXISTS receipts(t TEXT PRIMARY KEY,received TEXT)')
    db.execute('DELETE FROM receipts');columns={};total=0;global_schema=None
    paths=sorted(source.glob('page_[0-9][0-9][0-9][0-9][0-9].json'))[:receipt['pages']]
    for path in paths:
        guard();rr=read(path.with_name(path.stem+'_receipt.json'))
        if sha(path)!=rr['sha256']:raise ValueError('CACHE_PAGE_HASH_MISMATCH')
        data=read(path).get('quotes') or {}
        rows=data.get(receipt['params']['symbols'],[])
        if rows:
            schema=pa.Table.from_pandas(pd.DataFrame(rows),preserve_index=False).schema.remove_metadata()
            global_schema=schema if global_schema is None else pa.unify_schemas([global_schema,schema],promote_options='permissive')
        # Legacy attach_page_receipts covers every symbol present in a page.
        db.executemany('INSERT INTO receipts VALUES(?,?) ON CONFLICT(t) DO UPDATE SET received=excluded.received',
            ((r['t'],rr.get('source_received_at','UNKNOWN')) for values in data.values() for r in values))
        for row in rows:
            total+=1
            for key,value in row.items():
                info=columns.setdefault(key,{'count':0,'null':False,'types':set()})
                info['count']+=1
                if value is None:info['null']=True
                else:info['types'].add(type(value).__name__)
        db.commit()
    if total!=receipt['rows']:raise ValueError('PAGE_NORMALIZATION_ROW_COUNT_MISMATCH')
    numeric_float={k for k,v in columns.items() if v['types'] and v['types']<={'int','float'} and ('float' in v['types'] or v['null'] or v['count']<total)}
    if global_schema is not None:
        global_schema=pa.schema([pa.field(k,pa.float64() if k in numeric_float else global_schema.field(k).type) for k in columns]+
            [pa.field(k,pa.string()) for k in ('source_received_at','input_snapshot_completed_at','historical_network_received_at','quote_id','size_unit')])
    writer=None;written=0;tmp=target.with_suffix('.parquet.tmp')
    try:
        for path in paths:
            guard();rr=read(path.with_name(path.stem+'_receipt.json'))
            if sha(path)!=rr['sha256']:raise ValueError('CACHE_PAGE_HASH_MISMATCH')
            rows=(read(path).get('quotes') or {}).get(receipt['params']['symbols'],[])
            for offset in range(0,len(rows),batch_size):
                normalized=[]
                for row in rows[offset:offset+batch_size]:
                    original={k:row.get(k,float('nan')) for k in columns}
                    for k in numeric_float:original[k]=float(original[k]) if original[k] is not None else float('nan')
                    # Exact B11 digest intentionally uses its original JSON
                    # spacing/NaN semantics, distinct from engine canonical SHA.
                    qid=digest({'symbol':symbol,'original_record':original})
                    got=db.execute('SELECT received FROM receipts WHERE t=?',(row['t'],)).fetchone()
                    normalized.append({**original,'source_received_at':got[0] if got else 'UNKNOWN',
                        'input_snapshot_completed_at':receipt['last_source_received_at'],'historical_network_received_at':'UNKNOWN',
                        'quote_id':qid,'size_unit':'shares'})
                # Pandas writes its missing scalar NaNs as Parquet nulls. IDs
                # above retain the original pre-Parquet NaN representation.
                output_rows=[{k:None if isinstance(v,float) and math.isnan(v) else v for k,v in row.items()} for row in normalized]
                table=pa.Table.from_pylist(output_rows,schema=global_schema)
                if writer is None:writer=pq.ParquetWriter(tmp,global_schema,compression='snappy')
                if table.schema!=writer.schema:table=table.cast(writer.schema)
                writer.write_table(table);written+=len(normalized)
        if writer is not None:writer.close();writer=None;tmp.replace(target)
    finally:
        if writer is not None:writer.close()
        db.close()
    write(folder/'NORMALIZATION_PROOF.json',{'at':utc(),'source_rows':total,'written_rows':written,
        'global_float_columns':sorted(numeric_float),'all_raw_pages_hash_verified_twice':True,
        'last_page_timestamp_receipt_rule':True,'batch_size':batch_size,'sampling':False})
    return target if written else None

class StreamingMarket(B12Market):
    def __init__(self,output=None):
        super().__init__(output or ROOT/'data/portfolio_quotes')
        self.hashes={}
        self.profile_hook=None

    def pin_verified(self,path,expected=None):
        path=Path(path);key=str(path);stat=path.stat();identity=(stat.st_size,stat.st_mtime_ns)
        cached=self.hashes.get(key)
        if cached is None or cached[0]!=identity:
            actual=sha(path);self.hashes[key]=(identity,actual)
        else:actual=cached[1]
        if expected and actual!=expected:raise ValueError('PINNED_INPUT_HASH_CHANGED:'+key)
        self.used[key]=actual;return actual

    def quote_source(self,symbol,start,end,scope):
        guard();query=query_ticker(symbol)
        if self.profile_hook:self.profile_hook({'phase':'QUOTE_SOURCE_ACQUIRE','symbol':symbol,'start':str(start),'end':str(end),'scope':scope})
        for prior in self.previous:
            p=prior.get('params',{})
            if prior.get('symbol')!=symbol or prior.get('kind')!='quotes' or not prior.get('complete') or p.get('symbols')!=query or p.get('feed')!='sip':continue
            if pd.Timestamp(p['start'])>pd.Timestamp(start) or pd.Timestamp(p['end'])<pd.Timestamp(end):continue
            path=Path(prior['path'])/'data.parquet'
            if not path.exists() and prior.get('rows')!=0:continue
            actual=self.pin_verified(path) if path.exists() else None
            r={**prior,'old_cache_reused_read_only':True,'requested_start':str(start),'requested_end':str(end),
                'scope_b12':scope,'old_input_sha256':actual}
            self.state['cache_reuse_records'].append(r);self.save()
            return path if path.exists() else None,r
        rec=next(r for r in load_universe()['records']+load_universe()['references'] if r['symbol']==symbol)
        params={'symbols':query,'start':pd.Timestamp(start).isoformat(),'end':pd.Timestamp(end).isoformat(),'feed':'sip','asof':'-','limit':10000,'sort':'asc'}
        if pd.Timestamp(end)>pd.Timestamp.now(tz='UTC')-pd.Timedelta(minutes=20):raise ValueError('BASIC_DELAY_REQUIRED')
        if pd.Timestamp(start)<pd.Timestamp('2025-11-03T00:00:00Z'):raise ValueError('PRE_CHANGE_QUOTE_SIZE_UNVERIFIED_NOT_ALLOWED')
        key={'symbol':symbol,'identity_version':rec['identity_version'],'kind':'quotes','params':params,'session':scope,'version':'BEN_B1_PROBE_V1'}
        folder=self.output/'cache'/digest(key);folder.mkdir(parents=True,exist_ok=True);write(folder/'request.json',key)
        token=None;count=0;pages=0;received='UNKNOWN';complete=False;error=None
        for page in range(300):
            guard();p=folder/f'page_{page:05d}.json';rp=p.with_name(p.stem+'_receipt.json')
            if self.profile_hook:self.profile_hook({'phase':'SIP_QUOTE_PAGE_ACQUIRE','symbol':symbol,'page':page,'rows_received':count})
            if p.exists() and rp.exists():
                rr=read(rp)
                if sha(p)!=rr['sha256']:raise ValueError('CACHE_HASH_MISMATCH')
                result=read(p);self.state['cache_pages']+=1
            else:
                args=dict(params)
                if token:args['page_token']=token
                requested=utc()
                try:result=self.sdk.get('/stocks/quotes',data=args)
                except DataAccessError as exc:error=exc.code;break
                received=utc();write(p,result)
                rr={'request_started_at':requested,'source_received_at':received,'retrieved_at':received,
                    'source_published_at':'UNKNOWN','historical_network_received_at':'UNKNOWN','sha256':sha(p),
                    'rows':sum(len(v) for v in (result.get('quotes') or {}).values()),'next_page_token_present':bool(result.get('next_page_token'))}
                write(rp,rr);self.state['api_pages']+=1
            pages+=1;received=rr.get('source_received_at','UNKNOWN');count+=len((result.get('quotes') or {}).get(query,[]))
            token=result.get('next_page_token');self.save()
            if not token:complete=True;break
        receipt={**key,'cache_key':folder.name,'path':str(folder),'rows':count,'pages':pages,'complete':complete,
            'status':error or ('ACCESS_OK' if count else 'EMPTY_RESPONSE') if complete or error else 'PAGE_BUDGET_INCOMPLETE',
            'last_source_received_at':received,'known_at_historical':'UNKNOWN','size_unit':'shares',
            'historical_network_received_at':'UNKNOWN','event_timestamp_precision':'ORIGINAL_VENDOR_TIMESTAMP','old_quote_normalization_verified':False}
        write(folder/'summary.json',receipt)
        path=normalize_pages(symbol,receipt)
        if path:self.pin_verified(path)
        self.state['requests']=[r for r in self.state['requests'] if r.get('cache_key')!=folder.name]+[receipt]
        self.state['new_successful_http_pages']=self.state['api_pages'];self.save()
        if complete:self.previous.append(receipt)
        return path,receipt

class StreamingInputs(ContinuousInputs):
    def pin(self,path):
        # Static construction uses the original full SHA. Repeated immutable
        # minute datasets are verified once per process and fully at final QA.
        p=Path(path)
        if hasattr(self.market,'pin_verified'):self.pinned[str(p)]=self.market.pin_verified(p,self.pinned.get(str(p)))
        else:super().pin(p)

    def quotes(self,symbol,day,holding):
        c=self.clocks[day]
        start,end=(c.market_open,c.market_close) if holding else (c.market_close+pd.Timedelta(minutes=5,seconds=-5),c.market_close+pd.Timedelta(minutes=15))
        path,r=self.market.quote_source(symbol,start,end,'B12_HOLDING_RTH' if holding else 'B12_ENTRY_WINDOW')
        if path:self.pinned[str(path)]=self.market.used[str(path)]
        record={'symbol':symbol,'day':day,'purpose':'ACTUAL_HOLDING_RTH' if holding else 'CAUSAL_SIGNAL_ENTRY_WINDOW',
            'rows':None,'complete':r.get('complete'),'status':r.get('status'),'path':str(Path(r['path'])/'data.parquet'),
            'query_symbol':r['params'].get('symbols'),'receipt_time':r.get('last_source_received_at')}
        self.coverage.append(record)
        def events():
            yield from parquet_quotes(path,symbol,r,start,end,profile_hook=self.market.profile_hook)
            record['rows']=r['rows_in_requested_window']
        return events(),r
