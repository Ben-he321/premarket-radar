"""Real dense vendor normalization/order check; no Jan23 engine execution."""
import hashlib
import itertools
import time
from pathlib import Path
import pandas as pd
import pyarrow.parquet as pq
from src.ben_b1.replay import _json
from .inputs import normalize_pages, parquet_quotes
from .runtime import ROOT,OLD,read,write,sha,utc,guard

def run(version='v1'):
    started=time.monotonic();directory=ROOT/'engineering'/('real_input' if version=='v1' else 'real_input_'+version);directory.mkdir(parents=True,exist_ok=True)
    prior=read(OLD/'data/portfolio_quotes/PROBE_STATE.json')['requests']
    r=next(x for x in prior if x['symbol']=='NVDA' and x['kind']=='quotes' and x.get('complete') and x['params']['start'].startswith('2026-01-23T14:30:00'))
    original=Path(r['path'])/'data.parquet';before=sha(original)
    new=normalize_pages('NVDA',r,output=directory/'normalized_copy')
    def rows(path,size):
        for b in pq.ParquetFile(path).iter_batches(batch_size=size):yield from b.to_pylist()
    count=0;h=hashlib.sha256()
    for a,b in itertools.zip_longest(rows(original,4096),rows(new,997)):
        if a is None or b is None or _json(a)!=_json(b):
            write(directory/'ROW_MISMATCH.json',{'index':count,'original':a,'new':b})
            raise ValueError('REAL_NORMALIZATION_RECORD_MISMATCH')
        h.update(_json(a).encode());count+=1
        if count%65536==0:guard();print({'phase':'REAL_NORMALIZED_ROW_COMPARISON','rows':count},flush=True)
    digests=[]
    for size in (997,4096):
        h2=hashlib.sha256();n=0
        for e in parquet_quotes(original,'NVDA',dict(r),r['params']['start'],r['params']['end'],size):
            h2.update(_json(e).encode());n+=1
        digests.append({'batch_size':size,'count':n,'digest':h2.hexdigest()})
    same=digests[0]['count']==digests[1]['count'] and digests[0]['digest']==digests[1]['digest']
    result={'status':'PASS' if same and sha(original)==before else 'FAIL','at':utc(),'synthetic':False,
        'no_new_historical_engine_date_executed':True,'symbol':'NVDA','day':'2026-01-23','rows':count,'pages':r['pages'],
        'every_field_including_original_quote_id_and_receipt_equal':True,'source_sha256':before,
        'source_path':str(original),'normalized_copy':str(new),'records_digest':h.hexdigest(),'different_batch_sizes':digests,'elapsed_seconds':time.monotonic()-started}
    write(ROOT/'engineering'/('REAL_DENSE_INPUT_EQUIVALENCE.json' if version=='v1' else 'REAL_DENSE_INPUT_EQUIVALENCE_'+version+'.json'),result)
    if result['status']!='PASS':raise ValueError('REAL_BATCH_EQUIVALENCE_FAILED')
    print(result,flush=True)
if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--version',default='v1');run(p.parse_args().version)
