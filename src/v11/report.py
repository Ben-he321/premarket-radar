"""Report-only allowlist; never archive credentials, raw bars or databases."""
from pathlib import Path
import hashlib
import json
import subprocess
import zipfile
import pandas as pd
from .runtime import root,source,read,write,utc
from .data import snapshot

FORMULAS=[
('return_1','C/C[-1]-1','C; 2 closes','A, C direct; LEGACY equivalent close comparison'),
('return_5','C/C[-5]-1','C; 6 closes','C mean reversion, not independent momentum'),
('return_20','C/C[-20]-1','C; 21 closes','COMPUTED_UNUSED; feeds unused relative20'),
('return_60','C/C[-60]-1','C; 61 closes','COMPUTED_UNUSED'),
('ma5_gap','C/SMA(C,5)-1','C; 5 sessions','Column unused; A/LEGACY recompute SMA5 in signal'),
('ma10_gap','C/SMA(C,10)-1','C; 10 sessions','Column unused; LEGACY recomputes SMA10'),
('ma20_gap','C/SMA(C,20)-1','C; 20 sessions','A direct'),
('ma60_gap','C/SMA(C,60)-1','C; 60 sessions','A, B direct'),
('ma20_slope','SMA20/SMA20[-5]-1','C; 25 sessions','COMPUTED_UNUSED'),
('atr14_ratio','SMA14(max(H-L,abs(H-C[-1]),abs(L-C[-1])))/C','H,L,C; 15 sessions','COMPUTED_UNUSED; stop remains fixed 5%'),
('vol20','STD20(return_1), ddof=1','C; 21 sessions','COMPUTED_UNUSED'),
('downside_vol20','STD20(min(return_1,0)), ddof=1','C; 21 sessions; positive returns become zero','COMPUTED_UNUSED'),
('volume_ratio5','V/SMA(V[-1],5)','V; current + previous 5 sessions','A, B, C, LEGACY direct'),
('volume_ratio20','V/SMA(V[-1],20)','V; current + previous 20 sessions','COMPUTED_UNUSED'),
('dollar_volume20','SMA(C*V,20)','C,V; 20 sessions','COMPUTED_UNUSED; not the prior-volume order cap'),
('breakout20','C/MAX(H[-1],20)-1','C,H; current + previous 20 sessions','B direct'),
(' range_position20'.strip(),'(C-MIN(L,20))/(MAX(H,20)-MIN(L,20))','C,H,L; 20 sessions','COMPUTED_UNUSED'),
('gap','O/C[-1]-1','O,C; 2 sessions','COMPUTED_UNUSED as a signal; raw execution gap handling is separate'),
('intraday','C/O-1','O,C; 1 session','COMPUTED_UNUSED'),
('rsi14','100-100/(SMA14(max(deltaC,0))/SMA14(max(-deltaC,0)))','C; 15 sessions; simple averages, both flat -> missing; no loss and gain ->100','C direct'),
('relative20','return_20 - SPY_return_20','C, SPY C; 21 aligned sessions','COMPUTED_UNUSED'),
('drawdown60','C/MAX(C,60)-1','C; 60 sessions','COMPUTED_UNUSED'),
('market_up','SPY_C > SMA(SPY_C,60)','SPY C; 60 sessions; auxiliary boolean','C signal; HYBRID shared market regime selection'),
('observed','C is not missing','C; current session','All signal masks; data coverage eligibility')]

def factor_coverage():
    evidence=read(root()/'feature_run_evidence.json',[]);stats=read(source()/'research'/'statistics.json',{})
    usage={}
    for structure in snapshot()['old_config']['structures']:
        t=pd.read_parquet(source()/'research'/f'{structure}-base-trades.parquet')
        for family,count in t.strategy.str.split('-').str[0].value_counts().items():usage[f'{structure}/{family}']=int(count)
    lines=['# FACTOR_COVERAGE — actual computation and call-chain audit','',
           f'Generated {utc()}. Scope: all {len(evidence)} candidates, 22 numeric features plus 2 boolean helpers. No new factor is added to a trading rule.',
           '', 'Inputs C/O/H/L are current all-adjusted daily snapshots for research features; V is the provider volume on that adjustment basis. '+
           'They are aligned to NYSE sessions in New York; missing sessions stay missing. Ratios are decimal, not percent. '+
           'Rolling windows require full observations; infinities become missing. Current adjusted snapshots are NOT historical point-in-time vintages.',
           '', 'Call chain: src/watchlist/features.py:feature_frame → signal → V1 research diagnostics/choose/frozen intents → raw account replay. '+
           'V1.1 analysis.candidates calls the unchanged feature_frame/signal, then the shared raw-price kernel with a fixed $5500 reference account. '+
           'V1.1 paper.real_decision_inputs uses the unchanged FIXED_LEGACY rule; its ledger does not trade unused features.',
           '', '| Name | Formula | Inputs / lookback | Actual strategy use | Computation / this-run evidence | Independent effectiveness |',
           '|---|---|---|---|---|---|']
    for name,formula,inputs,use in FORMULAS:
        if name in ('market_up','observed'):proof='IMPLEMENTED; actual signal/eligibility calls, auxiliary columns'
        else:
            count=sum(x['features'][name]['nonmissing'] for x in evidence)
            proof=f'IMPLEMENTED; {count} nonmissing observations / {len(evidence)} symbols; feature_run_evidence.json per-column hashes'
        lines.append(f'| {name} | {formula} | {inputs} | {use} | {proof} | NOT_TESTED independently; no IC/ablation evidence |')
    lines+=['', 'Directly used features participated jointly in the old 12-config search, frozen selection and historical account fills. '+
            'This proves code-path execution, not independent factor validity. Computed-unused columns did not influence ranking or fills. '+
            'A/LEGACY SMA5 and LEGACY SMA10 are recomputed from close; the normalized ma5_gap/ma10_gap columns themselves are not read by signal.',
            '', 'Existing V1 completed base trades by structure/family (read-only evidence; not natural forward fills):',
            '```json',json.dumps(usage,indent=2), '```','',
            'Current runtime evidence: feature_run_evidence.json, candidate_cost_differences.csv, candidate_ranking_evidence.csv, '+
            'selection_restatement.csv, forward_preflight.json. Historical underlying evidence is pinned by v1_snapshot.json. '+
            'Engineering tests verify arithmetic, causality and execution, not an independent IC test or profitable factor.',
            '', '| Unconnected factor class | Status | Missing evidence |','|---|---|---|',
            ' | Value | NOT_IMPLEMENTED | No valuation dataset, point-in-time multiples or tests |',
 ' | Quality | NOT_IMPLEMENTED | No point-in-time profitability/leverage/accounting data |',
 ' | Growth | NOT_IMPLEMENTED | No point-in-time revenue/earnings revisions or growth data |',
 ' | Sentiment | NOT_IMPLEMENTED | No news/social/analyst sentiment feed or tests |',
            '', 'Standalone momentum research (including 1/5/20/60 sessions) remains a future independently preregistered hypothesis; '+
            'it is not folded into this round, tuned or added to paper trading. The 5-session return is currently a mean-reversion input.',
            '', '| QuantSkill | Downloaded / pinned | Reviewed | Structure borrowed | Algorithm executed | Real data validation |',
 '|---|---|---|---|---|---|',
 '| quant-factor-factory | YES; d0fae388 | YES | Catalog/causal adapter structure | Catalog import only; full generator NOT_EXECUTED, external core absent | Local features have real data evidence; the full skill algorithm is NOT_VALIDATED |',
 '| factor-orthogonalize | YES; 36972acf6 | YES | Training-only correlation idea | Full orthogonalization NOT_EXECUTED; local correlation is not residualization | No skill algorithm validation; V1 features_audit.json is local correlation only |',
 '| backtest-overfit | YES; c6c016221 | YES | DSR/PBO diagnostics | Pure DSR/PBO functions ran, not synthetic demo | V1 statistics.json + complete-config-date-matrix.parquet (hash pinned); diagnostic only, not a strategy certificate |',
            '', f'V1 recorded statistics: {stats.get("input_rows")} dates, {len(stats.get("input_columns",[]))} configuration columns; '+
            f'trial count {stats.get("total_trial_count")}; full search DSR: {stats.get("full_search_DSR")}.',
            'No skills were reinstalled this round. Reused structure does not mean its entire algorithm ran. '+
            'Source details: config/research_skills.json and docs/THIRD_PARTY_RESEARCH.md. Automatic monthly improvement is NOT_IMPLEMENTED.']
    text='\n'.join(lines)
    (root()/'FACTOR_COVERAGE.md').write_text(text,encoding='utf-8')

def build():
    factor_coverage();r=root();snap=snapshot()
    mismatches=[name for name,h in snap['v1_result_hashes'].items() if hashlib.sha256((source()/name).read_bytes()).hexdigest()!=h]
    write(r/'v1_preservation.json',{'checked_at':utc(),'files':len(snap['v1_result_hashes']),'changed':mismatches})
    code=Path(__file__).resolve().parents[2]
    versions={'generated_at':utc(),'branch':subprocess.check_output(['git','branch','--show-current'],cwd=code,text=True).strip(),
              'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=code,text=True).strip(),
              'files':{str(p.relative_to(code)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (code/'src/v11').glob('*.py')}}
    write(r/'source_versions.json',versions)
    mech=pd.read_csv(r/'mechanical_restatement.csv');sel=read(r/'candidate_restatement_summary.json',{});paper=read(r/'experimental_paper_status.json',{})
    lines=['# V1.1 actual results','',f'Generated {utc()}. Branch {versions["branch"]}; commit {versions["head"]}.',
      '', 'V1 results and all 66 candidates are preserved. No broker orders, paid models, new services or new strategies. '+
      'All 2017–2026 results below are observed-data engineering restatements. No old holdout reselection or champion promotion.',
      '', 'Implemented: shared quantity/fees/friction/ledger kernel, field-aware data views, minimal real SIP minute probes, '+
      'held-action interval audit, five issuer/SEC-supported missing dividend payment dates, original-fill accounting cost addback, '+
      '12 fixed-intent portfolio cost scenarios, separate development candidate/ranking restatement, factor call-chain audit, '+
      'transactional immutable experimental intents, delayed minute matching, reservations, stops, time exits, settlement, recovery and scheduler.',
      '', 'Historical SIP: '+str(read(r/'sip_checks.json',{}).get('historical',{}).get('status'))+'. Latest SIP: '+str(read(r/'sip_checks.json',{}).get('latest',{}).get('status'))+'. No fallback feed.',
      '', '| Structure | Case | V1 equity | V1.1 equity | Changed share counts among common entries |','|---|---|---:|---:|---:|']
    for _,x in mech.iterrows():lines.append(f'| {x.structure} | {x.case} | {x.old_equity:.2f} | {x.new_equity:.2f} | {x.quantity_changed} |')
    lines+=['',f'Development selection comparisons: {sel.get("selection_rows")}; changed {sel.get("changed_selections")}. '+
            'See candidate_ranking_evidence.csv for all training ranks/counts and candidate_cost_differences.csv for event quantities/net dollars. '+
            'Differences are measurement/cash rounding/payment timing and raw-vs-adjusted execution changes, not an alpha improvement.',
            'Old intent files contain signal dates, not true historically recorded creation/receipt timestamps. These unknown timestamps remain null; only new forward intents have actual created_at and received_at.',
            '', 'loss_attribution.csv separates symbol, year, family, holding rule, exit reason and frozen entry window. '+
            'Count domain is completed account trades; development events and selection rows are separate. PF uses dollar profits/losses. '+
            'Accounting cost addback uses exactly the original filled positions; it is not an executable zero-cost account. '+
            'Dividend entitlements/cash and open-position fees are account components and must not be confused with closed-trade price PnL.',
            '', '368 quarantined rows had numerically valid OHLC but zero volume/trade count and zero VWAP. '+
            'They are retained as reference-only rows, not filled observations. No positive-volume auxiliary-only rows required restoration. '+
            'SMCI suspension and CLSK halt have primary-source evidence; BATL/CCXI zero-activity cause remains unverified. '+
            'The exact dates and fields are saved in field_views and gap_and_actions_audit.json.',
            '', 'Daily SIP volume can include extended-hours trades whose conditions do not update daily OHLC. '+
            'The minute sample often has a different first RTH open. A daily or minute open is not guaranteed executable. '+
            'Sources: [Alpaca FAQ](https://docs.alpaca.markets/us/docs/market-data-faq) and [bars API](https://docs.alpaca.markets/us/reference/stockbars).',
            '', f'Experimental account snapshot: {json.dumps(paper,ensure_ascii=False)}',
            '', 'The live ledger contains only naturally created intents and subsequent real quotes. Synthetic lifecycle tests use temporary databases. '+
            'Zero live fills means the natural forward lifecycle remains UNVERIFIED; engineering tests do not replace it.',
            '', 'Remaining limits: provider action completeness is not independently certified for every date; complex acquiree actions are security-level blocks. '+
            'Current adjusted snapshots and present watchlist introduce vintage/survivorship bias. No standalone factor effectiveness validation. '+
            'All three base account restatements remain negative; experimental paper use is engineering-only. '+
            'No monthly or autonomous strategy improvement algorithm is implemented.',
            '', f'V1 result preservation: {len(snap["v1_result_hashes"])} file hashes checked; changed: {mismatches}.',
            '', 'Page: http://localhost:8513 . Persistent outputs: '+str(r)+'. Recovery/stop commands: docs/V1_1_RUNBOOK.md. '+
            'Data and ledger are local, surviving process restarts; no cloud backup is implied. ZIP contains report allowlist only.',
            '', 'Next hypotheses only (not run):',
            '1. Shrink small-sample individual selection toward shared evidence; new preregistered future development windows, fixed 12-rule budget, fail if date-block net expectancy and 25bp stress remain nonpositive. Standalone momentum effectiveness can be preregistered as input evidence, without inserting it into the current rules.',
            '2. Volatility-normalized exits/longer holding: separate future windows and small frozen grid, same 0.5% account risk and costs; fail if added complexity does not beat the frozen baseline under stress.',
            '3. Individual adaptation versus shared market regimes: separate factorial comparison on new future windows, bounded configuration count; fail on no robust net improvement. No automatic monthly improvement is currently implemented.']
    (r/'V1_1_RESULTS.md').write_text('\n'.join(lines),encoding='utf-8')
    names=['V1_1_RESULTS.md','FACTOR_COVERAGE.md','config_and_engine_version.json','cost_parity_tests.json',
           'engineering_checks.json','gap_and_actions_audit.json','held_action_intervals.json','market_sample_audit.json','sip_checks.json',
           'loss_attribution.csv','mechanical_restatement.csv','candidate_cost_differences.csv','candidate_ranking_evidence.csv',
           'selection_restatement.csv','candidate_restatement_summary.json','feature_run_evidence.json','experimental_paper_status.json',
           'forward_preflight.json','forward_input_status.json','source_versions.json','v1_preservation.json','TASK_STATE.json','tests.log']
    names+=['candidate_version.json','candidate_action_intervals.json','inherited_bundle.json','operational_checks.json']
    paths=[r/n for n in names if (r/n).is_file()]+list((r/'field_views').glob('*.json'))
    missing=[n for n in names if not (r/n).is_file()]
    manifest={'generated_at':utc(),'missing':missing,'files':{p.relative_to(r).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}
    write(r/'bundle_manifest.json',manifest);paths.append(r/'bundle_manifest.json')
    with zipfile.ZipFile(r/'verification_v1_1_bundle.zip','w',zipfile.ZIP_DEFLATED) as z:
        for p in paths:z.write(p,str(p.relative_to(r)))
    return manifest
