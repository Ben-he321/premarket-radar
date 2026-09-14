"""Fixed already-executed MRVL real campaign; engineering equivalence only."""
import argparse
import cProfile
import gc
import json
import pstats
import time
from pathlib import Path
from src.ben_b1_2 import samples
from src.ben_b1_2.shared import SharedReplayEngine
from src.ben_b1_2.compact import stream_hash
from src.ben_b1.b11_research import Inputs
from .execution import BoundedReplayEngine
from .runtime import ROOT, OLD, write, read, guard, utc

VERSION='compact_equivalence_v1'
NAME='sample_02_MRVL_2026-01-02_B_'+VERSION
MANIFEST=OLD/'samples'/VERSION/'tier_B'/NAME/'HOLDING_QUOTE_REQUESTS.json'

def component_proof(engine):
    p=engine._payload();a=dict(p['state']['archive']);a.pop('path')
    return {'at':engine.state['at'],'events':engine.state['events_processed'],
        'payload':{k:stream_hash(v) for k,v in p.items() if k!='state'},
        'state':{k:stream_hash(a if k=='archive' else v) for k,v in p['state'].items()},
        'cash':engine.ledger.cash,'fills':len(engine.ledger.fills),'errors':len(engine.state['errors'])}

def run(mode):
    output=ROOT/'engineering'/('dense_'+mode);output.mkdir(parents=True,exist_ok=True)
    base=SharedReplayEngine if mode=='original' else BoundedReplayEngine
    proofs=[];prof=cProfile.Profile();profile_events=0;started=time.monotonic()
    class Observed(base):
        def process(self,event):
            nonlocal profile_events
            capture=mode=='original' and event.get('kind')=='QUOTE' and profile_events<10000
            if capture:prof.enable()
            try:return super().process(event)
            finally:
                if capture:
                    prof.disable();profile_events+=1
                    if profile_events==10000:
                        prof.dump_stats(str(output/'first10000_real_quotes.prof'))
                        with (output/'first10000_real_quotes_profile.txt').open('w',encoding='utf-8') as f:pstats.Stats(prof,stream=f).strip_dirs().sort_stats('cumulative').print_stats(50)
        def run(self,events):
            guard();start=time.monotonic();result=super().run(events)
            proof=component_proof(self);proof.update(elapsed=time.monotonic()-start)
            proofs.append(proof);write(output/'BLOCK_PROOFS.json',proofs)
            print(json.dumps({'mode':mode,'at':proof['at'],'events':proof['events'],'cash':proof['cash'],'seconds':proof['elapsed']}),flush=True)
            return result
    samples.ReplayEngine=Observed;samples.deadline=guard
    inputs=Inputs()
    result=samples.run_sample(inputs,2,'B',VERSION,samples.PinnedHoldingQuoteProvider(MANIFEST),output)
    write(output/'ENGINEERING_RUN.json',{'finished_at':utc(),'elapsed_seconds':time.monotonic()-started,'mode':mode,
        'source_manifest':str(MANIFEST),'scope':'FIXED_EXISTING_REAL_MRVL_ENGINEERING_CASE_NOT_NEW_STRATEGY_ACCOUNT',
        'events':result['events_processed'],'buy_fills':result['buy_fills'],'sell_fills':result['sell_fills'],'errors':result['error_count']})
    if mode!='original':
        old=read(ROOT/'engineering/dense_original/BLOCK_PROOFS.json')
        def comparable(rows):return [{k:v for k,v in r.items() if k!='elapsed'} for r in rows]
        same=comparable(old)==comparable(proofs)
        write(ROOT/'engineering/DENSE_REAL_EQUIVALENCE.json',{'at':utc(),'status':'PASS' if same else 'FAIL','all_block_all_component_hashes_equal':same,
            'real_events':result['events_processed'],'block_count':len(proofs),'synthetic':False,'original_proofs':str(ROOT/'engineering/dense_original/BLOCK_PROOFS.json'),'new_proofs':str(output/'BLOCK_PROOFS.json')})
        if not same:raise ValueError('REAL_DENSE_COMPONENT_DIVERGENCE')

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['original','bounded']);run(p.parse_args().mode)
