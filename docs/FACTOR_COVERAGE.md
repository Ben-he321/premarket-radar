# FACTOR_COVERAGE — actual computation and call-chain audit

Generated 2026-09-11T00:54:57.345507+00:00. Scope: all 66 candidates, 22 numeric features plus 2 boolean helpers. No new factor is added to a trading rule.

Inputs C/O/H/L are current all-adjusted daily snapshots for research features; V is the provider volume on that adjustment basis. They are aligned to NYSE sessions in New York; missing sessions stay missing. Ratios are decimal, not percent. Rolling windows require full observations; infinities become missing. Current adjusted snapshots are NOT historical point-in-time vintages.

Call chain: src/watchlist/features.py:feature_frame → signal → V1 research diagnostics/choose/frozen intents → raw account replay. V1.1 analysis.candidates calls the unchanged feature_frame/signal, then the shared raw-price kernel with a fixed $5500 reference account. V1.1 paper.real_decision_inputs uses the unchanged FIXED_LEGACY rule; its ledger does not trade unused features.

| Name | Formula | Inputs / lookback | Actual strategy use | Computation / this-run evidence | Independent effectiveness |
|---|---|---|---|---|---|
| return_1 | C/C[-1]-1 | C; 2 closes | A, C direct; LEGACY equivalent close comparison | IMPLEMENTED; 114185 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| return_5 | C/C[-5]-1 | C; 6 closes | C mean reversion, not independent momentum | IMPLEMENTED; 113934 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| return_20 | C/C[-20]-1 | C; 21 closes | COMPUTED_UNUSED; feeds unused relative20 | IMPLEMENTED; 112995 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| return_60 | C/C[-60]-1 | C; 61 closes | COMPUTED_UNUSED | IMPLEMENTED; 110555 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| ma5_gap | C/SMA(C,5)-1 | C; 5 sessions | Column unused; A/LEGACY recompute SMA5 in signal | IMPLEMENTED; 113985 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| ma10_gap | C/SMA(C,10)-1 | C; 10 sessions | Column unused; LEGACY recomputes SMA10 | IMPLEMENTED; 113655 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| ma20_gap | C/SMA(C,20)-1 | C; 20 sessions | A direct | IMPLEMENTED; 113002 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| ma60_gap | C/SMA(C,60)-1 | C; 60 sessions | A, B direct | IMPLEMENTED; 110442 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| ma20_slope | SMA20/SMA20[-5]-1 | C; 25 sessions | COMPUTED_UNUSED | IMPLEMENTED; 112682 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| atr14_ratio | SMA14(max(H-L,abs(H-C[-1]),abs(L-C[-1])))/C | H,L,C; 15 sessions | COMPUTED_UNUSED; stop remains fixed 5% | IMPLEMENTED; 113325 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| vol20 | STD20(return_1), ddof=1 | C; 21 sessions | COMPUTED_UNUSED | IMPLEMENTED; 112938 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| downside_vol20 | STD20(min(return_1,0)), ddof=1 | C; 21 sessions; positive returns become zero | COMPUTED_UNUSED | IMPLEMENTED; 112938 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| volume_ratio5 | V/SMA(V[-1],5) | V; current + previous 5 sessions | A, B, C, LEGACY direct | IMPLEMENTED; 113919 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| volume_ratio20 | V/SMA(V[-1],20) | V; current + previous 20 sessions | COMPUTED_UNUSED | IMPLEMENTED; 112938 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| dollar_volume20 | SMA(C*V,20) | C,V; 20 sessions | COMPUTED_UNUSED; not the prior-volume order cap | IMPLEMENTED; 113002 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| breakout20 | C/MAX(H[-1],20)-1 | C,H; current + previous 20 sessions | B direct | IMPLEMENTED; 112938 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| range_position20 | (C-MIN(L,20))/(MAX(H,20)-MIN(L,20)) | C,H,L; 20 sessions | COMPUTED_UNUSED | IMPLEMENTED; 113002 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| gap | O/C[-1]-1 | O,C; 2 sessions | COMPUTED_UNUSED as a signal; raw execution gap handling is separate | IMPLEMENTED; 114185 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| intraday | C/O-1 | O,C; 1 session | COMPUTED_UNUSED | IMPLEMENTED; 114252 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| rsi14 | 100-100/(SMA14(max(deltaC,0))/SMA14(max(-deltaC,0))) | C; 15 sessions; simple averages, both flat -> missing; no loss and gain ->100 | C direct | IMPLEMENTED; 113325 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| relative20 | return_20 - SPY_return_20 | C, SPY C; 21 aligned sessions | COMPUTED_UNUSED | IMPLEMENTED; 112995 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| drawdown60 | C/MAX(C,60)-1 | C; 60 sessions | COMPUTED_UNUSED | IMPLEMENTED; 110442 nonmissing observations / 66 symbols; feature_run_evidence.json per-column hashes | NOT_TESTED independently; no IC/ablation evidence |
| market_up | SPY_C > SMA(SPY_C,60) | SPY C; 60 sessions; auxiliary boolean | C signal; HYBRID shared market regime selection | IMPLEMENTED; actual signal/eligibility calls, auxiliary columns | NOT_TESTED independently; no IC/ablation evidence |
| observed | C is not missing | C; current session | All signal masks; data coverage eligibility | IMPLEMENTED; actual signal/eligibility calls, auxiliary columns | NOT_TESTED independently; no IC/ablation evidence |

Directly used features participated jointly in the old 12-config search, frozen selection and historical account fills. This proves code-path execution, not independent factor validity. Computed-unused columns did not influence ranking or fills. A/LEGACY SMA5 and LEGACY SMA10 are recomputed from close; the normalized ma5_gap/ma10_gap columns themselves are not read by signal.

Existing V1 completed base trades by structure/family (read-only evidence; not natural forward fills):
```json
{
  "SHARED/B": 556,
  "SHARED/A": 127,
  "SHARED/C": 7,
  "PER_SYMBOL/A": 425,
  "PER_SYMBOL/B": 225,
  "HYBRID_REGIME/B": 426,
  "HYBRID_REGIME/A": 324,
  "HYBRID_REGIME/C": 16
}
```

Current runtime evidence: feature_run_evidence.json, candidate_cost_differences.csv, candidate_ranking_evidence.csv, selection_restatement.csv, forward_preflight.json. Historical underlying evidence is pinned by v1_snapshot.json. Engineering tests verify arithmetic, causality and execution, not an independent IC test or profitable factor.

| Unconnected factor class | Status | Missing evidence |
|---|---|---|
 | Value | NOT_IMPLEMENTED | No valuation dataset, point-in-time multiples or tests |
 | Quality | NOT_IMPLEMENTED | No point-in-time profitability/leverage/accounting data |
 | Growth | NOT_IMPLEMENTED | No point-in-time revenue/earnings revisions or growth data |
 | Sentiment | NOT_IMPLEMENTED | No news/social/analyst sentiment feed or tests |

Standalone momentum research (including 1/5/20/60 sessions) remains a future independently preregistered hypothesis; it is not folded into this round, tuned or added to paper trading. The 5-session return is currently a mean-reversion input.

| QuantSkill | Downloaded / pinned | Reviewed | Structure borrowed | Algorithm executed | Real data validation |
|---|---|---|---|---|---|
| quant-factor-factory | YES; d0fae388 | YES | Catalog/causal adapter structure | Catalog import only; full generator NOT_EXECUTED, external core absent | Local features have real data evidence; the full skill algorithm is NOT_VALIDATED |
| factor-orthogonalize | YES; 36972acf6 | YES | Training-only correlation idea | Full orthogonalization NOT_EXECUTED; local correlation is not residualization | No skill algorithm validation; V1 features_audit.json is local correlation only |
| backtest-overfit | YES; c6c016221 | YES | DSR/PBO diagnostics | Pure DSR/PBO functions ran, not synthetic demo | V1 statistics.json + complete-config-date-matrix.parquet (hash pinned); diagnostic only, not a strategy certificate |

V1 recorded statistics: 2560 dates, 13 configuration columns; trial count 19962; full search DSR: UNAVAILABLE_NONEXCHANGEABLE_SYMBOL_AND_ROLLING_TRIALS.
No skills were reinstalled this round. Reused structure does not mean its entire algorithm ran. Source details: config/research_skills.json and docs/THIRD_PARTY_RESEARCH.md. Automatic monthly improvement is NOT_IMPLEMENTED.