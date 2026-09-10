"""Audited optional GPL tools imported only as functions; never run their demos."""
import importlib.util
import sys
import hashlib
import numpy as np
from src.data.alpaca_config import PROJECT_ROOT


def module(relative,name):
    path=PROJECT_ROOT/'vendor'/relative
    if not path.exists():raise FileNotFoundError('PINNED_SKILL_NOT_INSTALLED:'+relative)
    spec=importlib.util.spec_from_file_location(name,path)
    obj=importlib.util.module_from_spec(spec);sys.modules[name]=obj;spec.loader.exec_module(obj)
    return obj


def audit_statistics(matrix,total_trial_count):
    result={'total_trial_count':total_trial_count,'full_search_DSR':'UNAVAILABLE_NONEXCHANGEABLE_SYMBOL_AND_ROLLING_TRIALS',
            'scope':'13 fixed candidate date-aggregated event diagnostics only; not a portfolio significance certificate',
            'input_rows':len(matrix),'input_columns':list(matrix.columns),'input_sha256':hashlib.sha256(matrix.to_csv().encode()).hexdigest()}
    try:
        factory=module('quant-factor-factory/quant-factor-factory/scripts/generate_factor_skill_batch.py','audited_factor_catalog')
        result['factory']={'status':'CATALOG_READ_WITH_LOCAL_CAUSAL_ADAPTER','bases':[x[0] for x in factory.BASES],
                           'full_generator':'UNAVAILABLE_EXTERNAL_REAL_DATA_FACTOR_PIPELINE_NOT_BUNDLED','synthetic_pipeline_used':False}
        dsr=module('backtest-overfit/backtest-overfit/scripts/deflated_sharpe.py','audited_dsr')
        pbo=module('backtest-overfit/backtest-overfit/scripts/pbo_cscv.py','audited_pbo')
        a=matrix.to_numpy(float)
        sr=np.array([dsr.sharpe_ratio(a[:,i]) for i in range(a.shape[1])])
        if not np.isfinite(a).all() or not np.isfinite(sr).all():
            result.update(dsr='UNAVAILABLE_DEGENERATE_CANDIDATES',pbo='UNAVAILABLE_DEGENERATE_CANDIDATES');return result
        best=int(np.argmax(sr))
        result['dsr_fixed_grid_diagnostic']=dsr.deflated_sharpe_ratio(a[:,best],n_trials=total_trial_count,all_trial_sharpes=sr).to_dict()
        # The upstream rank counts ties optimistically. Reject a degenerate block
        # rather than silently allowing an undefined candidate to disappear.
        blocks=np.array_split(a,8)
        if any(not np.isfinite(pbo._sharpe_cols(b)).all() for b in blocks):
            result['pbo']='UNAVAILABLE_ZERO_VARIANCE_BLOCKS_NO_CANDIDATE_DROPPED'
        else:
            scores=[pbo._sharpe_cols(b) for b in blocks]
            if any(len(np.unique(np.round(x,12)))<len(x) for x in scores):result['pbo']='UNAVAILABLE_RANK_TIES_UPSTREAM_NOT_CONSERVATIVE'
            else:result['pbo_fixed_grid_diagnostic']=pbo.probability_of_backtest_overfitting(a,n_blocks=8).summary()
    except (FileNotFoundError,ImportError) as exc:result['tools']='UNAVAILABLE:'+str(exc)
    return result
