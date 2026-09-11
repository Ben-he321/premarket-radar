"""Synthetic method calibration lives ONLY in a temporary test directory."""
import tempfile
import numpy as np
import pandas as pd
from scipy.signal import lfilter
from .runtime import protocol, root, read, write, sha, utc, check_deadline, state
from .statistics import distribution, by_adjust, resolution

def run():
    receipt=root()/'calibration_receipt.json'
    if receipt.exists():
        x=read(receipt)
        if sha(x['report'])!=x['report_sha256']:raise ValueError('CALIBRATION_REPORT_CHANGED')
        return x
    p=protocol();spec=p['calibration'];out=tempfile.mkdtemp(prefix='BenAI-V13-SYNTHETIC-METHOD-TEST-')
    from pathlib import Path
    out=Path(out);write(out/'SYNTHETIC_TEST_PROTOCOL.json',{'created_at':utc(),'protocol_hash':p['protocol_hash'],'spec':spec})
    pd.DataFrame(resolution(1980,500)+resolution(9,10000)).to_csv(out/'p_resolution_all_ranks.csv',index=False)
    rng=np.random.default_rng(spec['seed']);rows=[]
    n=spec['days'];h=spec['overlap_sessions'];start=utc()
    for panel in range(spec['panels']):
        check_deadline()
        market=lfilter([1],[1,-spec['ar_market']],rng.normal(0,.002,n+h))
        idio=lfilter([1],[1,-spec['ar_idiosyncratic']],rng.normal(0,.006,(9,66,n+h)),axis=-1)
        observed=rng.random(idio.shape)>=spec['missing_probability']
        stock=np.where(observed,idio+market,np.nan)
        daily=np.nanmean(stock,axis=1)
        overlap=lfilter(np.ones(h)/h,[1],daily,axis=-1)[:,h:h+n].T
        overlap[rng.random(overlap.shape)<spec['missing_probability']]=np.nan
        est,boot,blocks=distribution(overlap)
        centered=boot-est;ci=np.nanquantile(boot,[.025,.975],axis=0)
        for pattern in ['sparse_one','dense_nine']:
            for effect in spec['effects']:
                truth=np.full(9,effect) if pattern=='dense_nine' else np.r_[effect,np.zeros(8)]
                measured=est+truth
                pv=(1+(np.abs(centered)>=np.abs(measured)).sum(axis=0))/(len(boot)+1)
                q=by_adjust(pv.tolist())
                for j in range(9):
                    rows.append({'panel':panel,'pattern':pattern,'grid_effect':effect,'test':j,'truth':truth[j],
                                 'estimate':measured[j],'bias':est[j],'ci_low':ci[0,j]+truth[j],'ci_high':ci[1,j]+truth[j],
                                 'covers_truth':ci[0,j]<=0<=ci[1,j],'p':pv[j],'q_by':q[j],'blocks':blocks})
        if panel%10==0:state('SYNTHETIC_METHOD_CALIBRATION',panels=panel+1,total=spec['panels'],temporary_directory=str(out))
    f=pd.DataFrame(rows);f.to_csv(out/'synthetic_calibration_trials.csv',index=False)
    zero=f[(f.pattern=='dense_nine')&(f.grid_effect==0)]
    reject=float((zero.p<=.05).mean());coverage=float(zero.covers_truth.mean())
    aggregate=[]
    for (pattern,effect),g in f.groupby(['pattern','grid_effect']):
        target=g[g.truth!=0] if effect else g
        panelrates=target.groupby('panel').apply(lambda d:pd.Series({'coverage':d.covers_truth.mean(),'rejection':(d.p<=.05).mean()}),include_groups=False)
        aggregate.append({'pattern':pattern,'effect':effect,'tests':len(target),'mean_bias':float(target.bias.mean()),
                          'coverage_95':float(target.covers_truth.mean()),'nominal_rejection_or_power':float((target.p<=.05).mean()),
                          'by_rejection_or_power':float((target.q_by<=.05).mean()),
                          'coverage_panel_mc_se':float(panelrates.coverage.std()/np.sqrt(spec['panels'])),
                          'rejection_panel_mc_se':float(panelrates.rejection.std()/np.sqrt(spec['panels']))})
    passed=reject<=.10 and coverage>=.90
    report={'label':'SYNTHETIC_METHOD_TEST_ONLY_NOT_STRATEGY_PERFORMANCE','started_at':start,'completed_at':utc(),
            'protocol_hash':p['protocol_hash'],'spec':spec,'null_nominal_rejection':reject,'null_95_ci_coverage':coverage,
            'calibration_guard_passed':passed,'primary_mode':'LIMITED_CALIBRATION_EXPLORATORY' if passed else 'DESCRIPTION_ONLY_UNCALIBRATED',
            'results':aggregate,'limitations':['Fixed stationary MAR simulation is not proof for nonstationary constrained accounts.',
            'Nine tests share market shocks; Monte Carlo uncertainty uses 100 independent panels, not 900 independent tests.',
            'Rank-bootstrap is not calibrated here and stays descriptive. No actual strategy performance in this directory.']}
    write(out/'calibration_report.json',report)
    (out/'CALIBRATION.md').write_text('# 方法校准（仅合成测试）\n\n'+
        f'预冻结 100 个面板，66 证券共同市场、AR 时间相关、20 日重叠、10% 缺失；每次 10000 重采样。零效应名义拒绝率 {reject:.2%}，95% 区间覆盖 {coverage:.2%}，预定守门结果 {passed}。\n\n'+
        'V1.2 最小 p=1/501；BY 是 step-up，多项同时有小 p 仍可能通过。全部排名阈值保存在 p_resolution_all_ranks.csv。V1.3 九项主检验的最小 p=1/10001。\n\n'+
        pd.DataFrame(aggregate).to_string(index=False)+'\n\n这些数字不是股票收益；不代表对非平稳账户序列或经验秩 IC 完成普遍校准。',encoding='utf-8')
    x={'completed_at':utc(),'temporary_directory':str(out),'report':str(out/'calibration_report.json'),
       'report_sha256':sha(out/'calibration_report.json'),'calibration_guard_passed':passed,
       'artifact_hashes':{z.name:sha(z) for z in out.iterdir() if z.is_file()},'label':'EXTERNAL_SYNTHETIC_METHOD_TEST_REFERENCE_ONLY'}
    write(receipt,x);return x
