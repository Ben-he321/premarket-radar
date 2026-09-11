import gzip,json
import pandas as pd
from .runtime import *
from . import data,accounts,analysis

def freeze(data0):
    p=protocol();_,candidates,_=accounts.build_inputs(data0);dest=root()/'fixed_intents';dest.mkdir(exist_ok=True)
    receipt=root()/'fixed_intent_manifest.json'
    if receipt.exists():
        old=read(receipt)
        assert all(sha(dest/f'{k}.json.gz')==v['file_sha'] for k,v in old['accounts'].items())
        freeze_protocol(receipt,p);return
    manifest={}
    for case in accounts.cases():
        check_deadline();name=case['id'];orders=accounts.make_orders(candidates,p['days'],case['condition'],case['horizon'],case['seed'])
        expected=read(prior()/f'accounts/{name}.json')['intent_hash'];assert digest(orders)==expected,name
        path=dest/f'{name}.json.gz';path.write_bytes(gzip.compress(json.dumps(orders,separators=(',',':')).encode(),mtime=0))
        manifest[name]={'original_intent_hash':expected,'file_sha':sha(path),'intent_count':sum(map(len,orders.values()))}
    write(receipt,{'at':utc(),'accounts':manifest,'total':len(manifest),'basis':'Reconstruct only to verify original 486 intent hashes, then replay frozen files; no parameter selection'})
    freeze_protocol(receipt,p)
    state('ALL_486_ORIGINAL_INTENT_HASHES_MATCHED')

def freeze_protocol(receipt,p):
    path=root()/'RESTATEMENT_PROTOCOL.json'
    if path.exists():
        assert read(path)['new_kernel_sha']==sha(CODE/'src/v131/kernel.py');return
    write(path,{'at':utc(),'old_protocol_sha':sha(prior()/'CONTROLLED_PROTOCOL.json'),'old_protocol':p,'new_kernel_sha':sha(CODE/'src/v131/kernel.py'),'intent_manifest_sha':sha(receipt),'new_intents':False,'data_overlay':False,'replays':486,'reason_all_accounts':'Exit fee affects every order capital bound; deterministic final restatement per fixed account','missing_quote_guard':'Unchanged conservative missing-held guard even where halt independently verified','source_commit':__import__('subprocess').check_output(['git','rev-parse','HEAD'],cwd=CODE,text=True).strip()})

def differences():
    before={x['id']:x for x in read(prior()/'account_summaries.json')};after=read(root()/'account_summaries.json');rows=[]
    for r in after:
        name=r['id'];a=pd.read_csv(prior()/f'accounts/{name}-equity.csv',float_precision='round_trip');b=pd.read_csv(root()/f'accounts/{name}-equity.csv',float_precision='round_trip')
        oldt=pd.read_csv(prior()/f'accounts/{name}-trades.csv',float_precision='round_trip');newt=pd.read_csv(root()/f'accounts/{name}-trades.csv',float_precision='round_trip')
        keys=['symbol','signal_date','entry_date','exit_date','reason']
        joined=oldt[keys+['qty']].merge(newt[keys+['qty']],on=keys,how='outer',suffixes=('_old','_new'),indicator=True)
        common=joined[joined._merge=='both'];diff=(a.cash-b.cash).abs()>.00001
        rows.append({'account_id':name,'intent_hash_equal':before[name]['intent_hash']==r['intent_hash'],'old_net':before[name]['net_pnl_usd'],'new_net':r['net_pnl_usd'],'delta_net':r['net_pnl_usd']-before[name]['net_pnl_usd'],'old_min_cash':float(a.cash.min()),'new_min_cash':float(b.cash.min()),'minimum_available_cash':float(b.available_cash.min()),'cash_changed_sessions':int(diff.sum()),'first_changed_cash_date':a.loc[diff,'date'].iloc[0] if diff.any() else None,'matched_trades_qty_changed':int((common.qty_old!=common.qty_new).sum()),'old_only_trade_paths':int((joined._merge=='left_only').sum()),'new_only_trade_paths':int((joined._merge=='right_only').sum()),'old_trades':len(oldt),'new_trades':len(newt),'old_equity_sha':sha(prior()/f'accounts/{name}-equity.csv'),'new_equity_sha':sha(root()/f'accounts/{name}-equity.csv')})
    pd.DataFrame(rows).to_csv(root()/'fixed_intent_account_differences.csv',index=False)
    assert all(r['new_min_cash']>=0 and r['minimum_available_cash']>=0 and r['intent_hash_equal'] for r in rows)
    write(root()/'restatement_engineering.json',{'status':'PASS','accounts':486,'cash_nonnegative':True,'original_intents_unchanged':True,'at':utc()})

def run():
    if read(root()/'restatement_engineering.json',{}).get('status')=='PASS':return
    for name in ['input_snapshot.json','actions.json','calibration_receipt.json']:
        target=root()/name
        if not target.exists():target.write_bytes((prior()/name).read_bytes())
    values,_=data.prepare();freeze(values);accounts.run(values);analysis.run();differences();state('FIXED_RESTATEMENT_COMPLETE')

if __name__=='__main__':run()
