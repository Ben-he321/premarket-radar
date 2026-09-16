"""Acquire only preregistered F0 slice requests, one at a time, with real quotes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from filelock import FileLock
from src.futures_f0.acquisition import Continuation
from src.futures_f0.budget import BudgetGate
from src.futures_f0.data import Request
from src.futures_f0.runtime import canonical_hash, digest, write_json, utcnow


def main():
    p=argparse.ArgumentParser();p.add_argument('--round',required=True);p.add_argument('--plan',required=True)
    args=p.parse_args();r=Path(args.round).resolve();plan_path=Path(args.plan).resolve()
    if not plan_path.is_relative_to(r): raise ValueError('PLAN_OUTSIDE_CURRENT_ROUND')
    plan=json.loads(plan_path.read_text(encoding='utf-8-sig'))
    if plan['slice_freeze_sha256']!=digest(r/'ENGINEERING_SLICE_FREEZE.json'):
        raise ValueError('ENGINEERING_SLICE_FREEZE_CHANGED')
    with FileLock(str(r/'research.lock'),timeout=0):
        run=Continuation(r)
        budget=BudgetGate(run.config.data_dir,r/'billing/credit_evidence_manifest.json',
            deadline=run.state['hard_deadline_utc'],round_state=r/'ROUND_STATE.json',authorization=r/'AUTHORIZATION.json')
        result=dict(started_at=utcnow().isoformat(),plan_sha256=digest(plan_path),requests=[],status='RUNNING')
        output=r/'acquisition_runs'/plan_path.name
        if output.exists(): raise ValueError('ACQUISITION_ATTEMPT_ALREADY_RECORDED_REVIEW_BEFORE_RESUME')
        write_json(output,result)
        try:
            for item in plan['requests']:
                run.guard.check({'stage':'acquisition_plan','completed':len(result['requests'])})
                request=Request(tuple(item['symbols']),item['schema'],item['start'],item['end'])
                params=request.parameters();identity=canonical_hash(params)
                # Explicit free metadata condition evidence distinguishes vendor
                # collection failures from a fully queried interval with no bars.
                dates=run._symbol_parameters(request)
                condition=run.metadata('metadata.get_dataset_condition',dict(dataset='GLBX.MDP3',
                    start_date=dates['start_date'],end_date=dates['end_date']))
                raw=run.config.data_dir/'vendor_cache'/(identity+'.csv')
                if not raw.exists():
                    estimate=run.client.estimate(request)
                    write_json(r/'pre_download_quotes'/(identity+'.json'),estimate,immutable=True)
                acquired=run.acquire(request,budget)
                entry=dict(request=params,receipt=acquired['receipt'],action=run.last_acquisition_action,
                    condition_sha256=canonical_hash(condition),at=utcnow().isoformat())
                result['requests'].append(entry);write_json(output,result)
                print(json.dumps(dict(schema=request.schema,symbols=request.symbols,start=request.start,
                    end=request.end,action=entry['action'],bytes=entry['receipt']['bytes'],
                    listed_usd=entry['receipt'].get('quoted_cost_usd')),ensure_ascii=True),flush=True)
            result['status']='REQUEST_PLAN_COMPLETED_RAW_NOT_AUTOMATICALLY_QUALIFIED'
        except BaseException as e:
            result['status']='STOPPED';result['error_type']=type(e).__name__
            result['error']=str(e) if isinstance(e,(ValueError,RuntimeError)) else 'UNEXPECTED_ERROR_REVIEW_LOCAL_CODE'
            raise
        finally:
            result.update(stopped_at=utcnow().isoformat(),budget=budget.snapshot(),
                peak_rss_bytes=run.guard.peak_rss,minimum_available_bytes=run.guard.min_available)
            write_json(output,result)
            prefix=json.loads((r/'preservation/BUDGET_PREFIX.json').read_text(encoding='utf-8-sig'))
            journal=run.config.data_dir/'credit_budget/journal.jsonl';raw=journal.read_bytes()
            write_json(r/'BUDGET_APPEND_EXPORT.json',dict(original_prefix=prefix,
                prefix_unchanged=__import__('hashlib').sha256(raw[:prefix['bytes']]).hexdigest()==prefix['sha256'],
                journal_sha256=digest(journal),new_events=[json.loads(x) for x in raw[prefix['bytes']:].splitlines()]))
            run.close()


if __name__=='__main__':main()
