"""Evidence-driven qualification for a fixed continuous futures engineering slice.

Dates are reconstructed from actual CME scheduled status/reset cohorts. This
is an observed calendar, not a claim that a future calendar publication existed.
Known supplier/customer publication timestamps remain unknown. All booleans
emitted by this module are computed from retained inputs, never read as approval.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import csv
import heapq
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .contract_snapshot import compare_definition
from .runtime import canonical_hash, digest
from .vendor import RawSource, timestamp_ns, iso_ns, NANO

ZONE=ZoneInfo('America/Chicago')
DAY=86400*NANO
_ACTIVE_CONTEXT = None


def reference(row):
    return {k:row[k] for k in ('source_sha256','source_line','raw_record_sha256')}


def read_bound(entry, root):
    p=Path(entry['path']).resolve()
    if not p.is_relative_to(Path(root).resolve()) or not p.is_file() or digest(p)!=entry['sha256']:
        raise ValueError('QUALIFICATION_EVIDENCE_PATH_OR_HASH_CHANGED')
    return json.loads(p.read_text(encoding='utf-8-sig'))


def load_context(evidence, *, guard=None):
    global _ACTIVE_CONTEXT
    value=evidence.read(kind='F0_CONTINUOUS_SOURCE_CONTEXT')
    root=value['data_root']
    if value['scope']==dict(start='2024-06-17',end='2024-10-04'):
        addendum=read_bound(value['warmup_addendum'],root)
        freeze=read_bound(value['slice_freeze'],root)
        if (addendum.get('kind')!='F0_ENGINEERING_WARMUP_ADDENDUM' or
            addendum.get('original_freeze_sha256')!=value['slice_freeze']['sha256'] or
            addendum.get('additional_warmup_sessions')!=dict(start='2024-06-17',end='2024-06-28') or
            addendum.get('original_execution_slice')!=dict(start='2024-07-01',end='2024-10-04') or
            addendum.get('first_open')!='2024-06-16T22:00:00Z' or addendum.get('account_results_seen') is not False):
            raise ValueError('PREDECLARED_WARMUP_ADDENDUM_REQUIRED')
    elif value['scope']!=dict(start='2024-07-01',end='2024-10-04'):
        raise ValueError('UNAPPROVED_ENGINEERING_SCOPE')
    if not Path(evidence.path).resolve().is_relative_to(Path(root).resolve()):
        raise ValueError('QUALIFICATION_CONTEXT_OUTSIDE_DATA_ROOT')
    key=(str(evidence.path.resolve()),evidence.sha256)
    if _ACTIVE_CONTEXT is None or _ACTIVE_CONTEXT[0]!=key:
        _ACTIVE_CONTEXT=(key,SourceSet(value['sources'],value['condition_entries'],root=root,guard=guard))
    catalog=_ACTIVE_CONTEXT[1]
    catalog.guard=guard
    catalog.assert_unchanged()
    return value,catalog


def load_rules(entry,root):
    rules=read_bound(entry,root)
    if rules.get('kind')!='F0_SP500_DATED_RULES' or rules.get('mock') is not False:
        raise ValueError('DATED_RULE_SOURCE_REVIEW_REQUIRED')
    required={'settlement_window','july_settlement','labor_clearing','ES_rule','MES_rule','nyse_2024','normal_hours'}
    if set(rules['sources'])!=required:raise ValueError('MISSING_REQUIRED_EXCHANGE_RULE_SOURCE')
    for source in rules['sources'].values():
        raw=read_bound(source,root)
        if (not source['url'].startswith(('https://www.cmegroup.com/','https://ir.theice.com/')) or raw['source_url']!=source['url']
            or raw.get('method')!='WEB_PRIMARY_SOURCE_RETRIEVAL' or not raw.get('result')):
            raise ValueError('PRIMARY_RULE_DOCUMENT_ASSOCIATION_MISMATCH')
    if (rules['ordinary_settlement_time_chicago']!='15:00:00'
        or rules['ordinary_rule_effective']!='2020-10-26'
        or rules['special_settlement_times']!={'2024-07-03':'12:00:00'}
        or rules['no_new_settlement_dates']!=['2024-07-04','2024-09-02']):
        raise ValueError('REVIEWED_SP500_RULE_SCOPE_MISMATCH')
    return rules


class SourceSet:
    def __init__(self, entries, condition_entries, *, root, guard=None):
        self.root=Path(root).resolve();self.guard=guard;self.sources=[];self.conditions={}
        self.condition_entries=condition_entries;self.filtered={};self.retained_count=0;self.calendar_cache={}
        if not 1<=len(entries)<=128:raise ValueError('QUALIFICATION_SOURCE_COUNT_BOUNDARY')
        for item in condition_entries:
            value=read_bound(item,root)
            if (value.get('method')!='metadata.get_dataset_condition' or value.get('http_status')!=200
                    or value.get('result_sha256')!=canonical_hash(value.get('result'))):
                raise ValueError('DATASET_CONDITION_EVIDENCE_INVALID')
            parameters=value.get('parameters',value.get('request',{}))
            if parameters.get('dataset')!='GLBX.MDP3':
                raise ValueError('DATASET_CONDITION_WRONG_DATASET')
            for row in value['result']:
                if not parameters['start_date']<=row['date']<=parameters['end_date']:
                    raise ValueError('DATASET_CONDITION_OUTSIDE_REQUEST_DATES')
                self.conditions.setdefault(row['date'],[]).append(row['condition'])
        self.proofs={}
        for entry in entries:
            path=Path(entry['path']).resolve();receipt=Path(entry['receipt_path']).resolve()
            if not path.is_relative_to(self.root) or not receipt.is_relative_to(self.root):
                raise ValueError('QUALIFICATION_RAW_PATH_OUTSIDE_ROOT')
            if digest(receipt)!=entry['receipt_sha256']:raise ValueError('QUALIFICATION_RECEIPT_CHANGED')
            source=RawSource.from_receipt(path,receipt,price_encoding=entry['price_encoding'])
            value=source.validate()
            if value.get('mock') or value.get('is_mock'):raise ValueError('MOCK_NOT_REAL_QUALIFICATION')
            if value['sha256']!=entry['source_sha256']:raise ValueError('QUALIFICATION_RAW_HASH_CHANGED')
            self.sources.append((source,value))

    def assert_unchanged(self):
        for item in self.condition_entries:read_bound(item,self.root)
        for source,receipt in self.sources:
            if source.validate()!=receipt:raise ValueError('QUALIFICATION_SOURCE_CHANGED')
        if self.guard:self.guard.check({'stage':'qualified_context_hash_recheck','retained_records':self.retained_count})

    def _complete_request(self, source, receipt):
        key=receipt['request_sha256']
        if key in self.proofs:return self.proofs[key]
        count=0;retained=[]
        for row in source.records(guard=self.guard):
            if row['parse_status']!='PARSED_VENDOR_RECORD':raise ValueError('UNPARSED_RAW_RECORD_IN_QUALIFICATION')
            count+=1
            # Retain only the three statistics used here, plus bounded daily
            # inputs. Every raw record is parsed, counted and hashed first.
            if row['schema']!='statistics' or row['stat_type'] in (3,17,18):
                retained.append(row)
                if self.retained_count+len(retained)>30000:
                    raise ValueError('QUALIFICATION_FILTERED_RECORD_CACHE_BOUNDARY')
        estimate=receipt.get('authenticated_estimate',{})
        if (estimate.get('request_sha256')!=key or estimate.get('get_record_count')!=count):
            raise ValueError('REAL_QUOTE_RECORD_COUNT_DOES_NOT_MATCH_COMPLETE_RESPONSE')
        proof=dict(request_sha256=key,raw_sha256=receipt['sha256'],records=count,
                   quote_record_count=estimate['get_record_count'])
        self.proofs[key]=proof
        self.filtered[key]=tuple(retained);self.retained_count+=len(retained)
        return proof

    def interval_coverage(self, contract, schema, start, end):
        spans=[];proofs=[]
        for source,r in self.sources:
            p=r['request'];a,b=timestamp_ns(p['start']),timestamp_ns(p['end'])
            if p['schema']!=schema or contract not in p['symbols'].split(',') or b<=start or a>=end:continue
            proofs.append(self._complete_request(source,r));spans.append((a,b))
        cursor=start
        for a,b in sorted(spans):
            if a>cursor:break
            cursor=max(cursor,b)
        # Dataset conditions apply to the used interval, not unused Saturdays
        # elsewhere inside a monthly request. A degraded used date still fails.
        dates=[];day=datetime.fromisoformat(iso_ns(start).replace('Z','+00:00')).date()
        while timestamp_ns(day.isoformat()+'T00:00:00Z')<end:
            dates.append(dict(date=str(day),conditions=self.conditions.get(str(day),[])))
            day+=timedelta(days=1)
        available=all(x['conditions'] and all(c=='available' for c in x['conditions']) for x in dates)
        return dict(status='NOT_REQUESTED' if cursor<end else
                    'COMPLETE_REQUEST_AVAILABLE_DATASET' if available else 'DATASET_DATE_NOT_AVAILABLE',
                    request_complete=cursor>=end,
                    requested_start=iso_ns(start),requested_end=iso_ns(end),proofs=proofs,dates=dates)

    def rows(self, schema, *, identity=None, before=None, after=None):
        index='ts_event_ns' if schema.startswith('ohlcv-') else 'ts_recv_ns'
        def stream(source):
            receipt=next(r for s,r in self.sources if s==source)
            self._complete_request(source,receipt)
            for row in self.filtered[receipt['request_sha256']]:
                if row['parse_status']!='PARSED_VENDOR_RECORD':raise ValueError('RAW_PARSE_FAILURE_IN_QUALIFICATION')
                if identity and (row['publisher_id'],row['instrument_id'])!=identity:continue
                if before is not None and row[index]>before:continue
                if after is not None and row[index]<after:continue
                yield row
        streams=[stream(s) for s,r in self.sources if r['request']['schema']==schema
                 and (after is None or timestamp_ns(r['request']['end'])>after)
                 and (before is None or timestamp_ns(r['request']['start'])<=before)]
        yield from heapq.merge(*streams,key=lambda x:(x[index],x['source_sha256'],x['source_line']))


def realized_calendar(catalog, *, contract_id, identity):
    """Replay reset cohorts; midnight state copies are not fresh reset events."""
    cache_key=(contract_id,identity)
    if cache_key in getattr(catalog,'calendar_cache',{}):return catalog.calendar_cache[cache_key]
    cohorts=[];current=None;opened=None;last_event=-1;last_payload=None;seen={};last_capture=-1
    for row in catalog.rows('status',identity=identity):
        event=row['ts_event_ns'];payload=tuple(row[k] for k in ('action','reason','trading_event','is_trading','is_quoting'))
        if event<=last_event and payload in seen.get(event,set()):continue
        if event<last_event or (event==last_event and row['ts_recv_ns']==last_capture):
            raise ValueError('STATUS_LATE_OR_CONFLICTING_EVENT_REQUIRES_REVIEW')
        # e.g. CME new-price-indication then trading share ts_event but have
        # distinct ordered capture events. They are a progression, not a tie.
        seen.setdefault(event,set()).add(payload);last_capture=row['ts_recv_ns']
        if len(seen)>4096:raise ValueError('STATUS_EVENT_DEDUP_BOUNDARY')
        last_event,last_payload=event,payload
        if row['trading_event']==2:
            if current is not None and (current['segments'] or opened is not None):
                raise ValueError('SESSION_RESET_WITHOUT_SCHEDULED_CLOSE')
            current=dict(reset=reference(row),reset_event_at=row['ts_event'],reset_capture_at=row['ts_recv'],
                         segments=[],status_records=[],unexpected=[],confirmed_ns=row['ts_recv_ns'])
            opened=None
        if current is None:continue
        current['status_records'].append(dict(**reference(row),event_at=row['ts_event'],capture_at=row['ts_recv'],
              action=row['action'],reason=row['reason'],trading_event=row['trading_event'],is_trading=row['is_trading']))
        current['confirmed_ns']=max(current['confirmed_ns'],row['ts_recv_ns'])
        if len(current['status_records'])>512:raise ValueError('DATED_STATUS_COHORT_BOUNDARY')
        if row['reason'] not in (0,1) or row['action'] in (8,9,10,15):
            current['unexpected'].append(reference(row))
        if row['action']==7 and row['is_trading']=='Y' and row['reason']==1 and opened is None:
            opened=event
        # Unscheduled halts remain inside the session's price interval. They
        # block execution proxies separately, never erase OHLCV or trading time.
        if row['is_trading']=='N' and row['reason']==1 and opened is not None:
            if event<=opened:raise ValueError('INVALID_STATUS_TRADING_SEGMENT')
            current['segments'].append([iso_ns(opened),iso_ns(event)]);opened=None
        if row['action']==12 and row['reason']==1:
            if current['segments']:
                session=datetime.fromisoformat(row['ts_event'].replace('Z','+00:00')).astimezone(ZONE).date()
                current.update(contract_id=contract_id,session=str(session),
                    calendar_confirmed_at=iso_ns(current.pop('confirmed_ns')),
                    date_basis='SCHEDULED_CME_CLOSE_CHICAGO_DATE_WITHIN_RESET_COHORT',
                    calendar_basis='GLBX_STATUS_SESSION_RESET_COHORT',close_record=reference(row))
                first=timestamp_ns(current['segments'][0][0]);last=timestamp_ns(current['segments'][-1][1])
                if len(current['segments'])>4 or last-first>72*3600*NANO:
                    raise ValueError('BOUNDED_STATUS_SESSION_ENVELOPE')
                if any(timestamp_ns(a)%(60*NANO) or timestamp_ns(b)%(60*NANO) for a,b in current['segments']):
                    raise ValueError('STATUS_SESSION_NEEDS_SUBMINUTE_PRICES')
                coverage=catalog.interval_coverage(contract_id,'status',timestamp_ns(current['reset_capture_at']),
                                                    timestamp_ns(current['calendar_confirmed_at'])+1)
                # Retain the observed calendar and its failed coverage gate.
                # Qualification later rejects this date; one degraded date
                # must not erase or block all other observed cohorts.
                current['coverage_qualified']=coverage['status']=='COMPLETE_REQUEST_AVAILABLE_DATASET'
                current['request_coverage']=coverage;cohorts.append(current)
            current=None;opened=None
    for a,b in zip(cohorts,cohorts[1:]):
        a['next_session']=b['session'];a['next_open']=b['segments'][0][0]
    if cohorts:cohorts[-1].update(next_session=None,next_open=None)
    if hasattr(catalog,'calendar_cache'):catalog.calendar_cache[cache_key]=cohorts
    return cohorts


def fixed_sp500_boundaries(contract_id, definition, existence, *, study_start, rules,eligible_from='2024-07-01'):
    """Rule-derived cash-settled expiry, corroborated by actual CME definition."""
    if contract_id not in ('ESU4','ESZ4','MESU4','MESZ4'):
        raise ValueError('OUTSIDE_PREDECLARED_ENGINEERING_CONTRACTS')
    month=9 if 'U4' in contract_id else 12
    first=date(2024,month,1);expiry=first+timedelta(days=(4-first.weekday())%7+14)
    expiry_at=datetime.combine(expiry,time(8,30),ZONE)
    if timestamp_ns(expiry_at)!=definition['expiration_ns']:raise ValueError('RULE_VENDOR_EXPIRATION_MISMATCH')
    if definition['activation_ns'] is None or definition['activation_ns']>timestamp_ns(study_start):
        raise ValueError('CONTRACT_NOT_ENABLED_BY_STUDY_START')
    if existence is None or existence['ts_event_ns']+3600*NANO>timestamp_ns(study_start) or not existence['volume']:
        raise ValueError('OBSERVED_PREEXISTENCE_REQUIRED_NOT_ACTIVATION_AS_LISTING')
    # These exact five-day intervals contain no CME/NYSE closure; dates/rules
    # are separately archived in the evidence, not inferred from profitability.
    safe=expiry-timedelta(days=7)
    return dict(actual_first_trade_date=None,eligible_from=eligible_from,
        eligibility_basis='OBSERVED_PREEXISTENCE_BEFORE_RESEARCH_START',existence_record=reference(existence),
        definition_record=reference(definition),last_trade=str(expiry),last_trade_at=expiry_at.isoformat(),
        safe_exit_session=str(safe),boundary_basis='CME_THIRD_FRIDAY_RULE_CORROBORATED_BY_VENDOR_EXPIRATION',
        first_notice='NOT_APPLICABLE_CASH_SETTLED',broker_boundary='NOT_APPLICABLE_NO_BROKER_ASSUMPTION_LAYER',
        rules=rules)


def limits_for_session(catalog, *, identity, segments):
    """Conservative whole-session proxy: retain every observed limit update."""
    first=timestamp_ns(segments[0][0]);last=timestamp_ns(segments[-1][1])
    prior={};events=[]
    for row in catalog.rows('statistics',identity=identity,before=last):
        if row['stat_type'] not in (17,18):continue
        item=dict(**reference(row),capture_at=row['ts_recv'],event_at=row['ts_event'],
                  stat_type=row['stat_type'],action=row['update_action'],price=row['price'])
        if row['ts_recv_ns']<=first:prior[row['stat_type']]=item
        else:events.append(item)
        if len(events)>20000:raise ValueError('BOUNDED_LIMIT_UPDATES')
    if set(prior)!={17,18}:return dict(status='LIMITS_UNKNOWN_AT_OPEN',opening=prior,events=events)
    if any(x['price'] is None or x['action']!=1 for x in prior.values()):
        return dict(status='INVALID_OPENING_LIMITS',opening=prior,events=events)
    return dict(status='LIMIT_UPDATES_RETAINED',opening=prior,events=events)


def first_clearing_final(catalog,identity,session,cutoff):
    """First final is actionable at its capture; later revisions gate this adapter.

    This deliberately does not pretend the one-settlement-per-day account API
    supports retroactive clearing revisions. All acquired matching messages are
    inspected, including a same-capture batch before selecting its candidate.
    """
    from .settlement import SettlementState
    state=SettlementState();first=None;problem=None;refs=[];batch=[];at=None
    def consume(group):
        nonlocal first,problem
        for row in group:state.add(row)
        row,status=state.account_candidate()
        if first is None and row is not None:first=row
        elif first is not None and (row is None or Decimal(row['price'])!=Decimal(first['price'])):
            problem='CLEARING_REVISION_AFTER_FIRST_FINAL_REQUIRES_EVENT_ACCOUNT_ADAPTER:'+status
    for row in catalog.rows('statistics',identity=identity,before=cutoff):
        if row['stat_type']!=3 or row['reference_session_hint']!=session:continue
        if at is not None and row['ts_recv_ns']!=at:consume(batch);batch=[]
        at=row['ts_recv_ns'];batch.append(row)
        refs.append(dict(**reference(row),capture_at=row['ts_recv'],event_at=row['ts_event'],
                         flags=row['stat_flags'],action=row['update_action'],price=row['price']))
        if len(refs)>512:raise ValueError('SETTLEMENT_AUDIT_VERSION_BOUNDARY')
    if batch:consume(batch)
    if first is None:problem=state.account_candidate()[1]
    return first,dict(status='FIRST_FINAL_WITHOUT_LATER_ECONOMIC_REVISION' if problem is None else problem,
       first_record=reference(first) if first else None,versions=refs,
       audit_cutoff=iso_ns(cutoff),audit_cutoff_is_trade_decision=False,
       supported='FIRST_FINAL_CAPTURE_ONLY_LATER_REVISIONS_ARE_QUALIFICATION_BLOCKERS')


def execution_evidence(catalog,identity,cohort,prices,*,tick_size=None):
    """Qualify existing daily proxies, keeping observed status and limit evidence.

    The open is the frozen session-open proxy, never a historical quote fill.
    A missing first bucket or unknown opening constraints denies that proxy.
    Intraday uncertainty never retroactively removes the opening permission.
    """
    first=timestamp_ns(cohort['segments'][0][0]);last=timestamp_ns(cohort['segments'][-1][1])
    reset=timestamp_ns(cohort['reset_event_at']);prior={};events=[];seen={};conflicts=[]
    for row in catalog.rows('statistics',identity=identity,before=last,after=reset):
        if row['stat_type'] not in (17,18):continue
        key=row['stat_type'];payload=(row['price'],row['update_action']);at=row['ts_event_ns']
        old=seen.get(key)
        if old is not None:
            if at<old['event']:
                if payload==old['payload']:continue
                conflicts.append(dict(reason='LATE_LIMIT_CHANGE',capture_at=row['ts_recv'],record=reference(row)))
            if row['ts_recv_ns']==old['capture'] and payload!=old['payload']:
                conflicts.append(dict(reason='SAME_CAPTURE_LIMIT_CONFLICT',capture_at=row['ts_recv'],record=reference(row)))
        seen[key]=dict(event=at,capture=row['ts_recv_ns'],payload=payload)
        item=dict(**reference(row),capture_at=row['ts_recv'],event_at=row['ts_event'],
                  stat_type=key,action=row['update_action'],price=row['price'])
        if row['ts_recv_ns']<=first:prior[key]=item
        else:events.append(item)
    opening=[x for x in cohort['status_records'] if x['action']==7 and x['reason']==1
             and x['is_trading']=='Y' and timestamp_ns(x['event_at'])==first]
    # Metadata capture follows exchange transition by milliseconds. We retain
    # both; the frozen daily fill uses a model-time open, not observed delivery.
    reasons=[]
    if not opening:reasons.append('SCHEDULED_TRADING_OPEN_NOT_OBSERVED')
    known=(set(prior)=={17,18} and all(x['action']==1 and x['price'] is not None for x in prior.values()))
    if not known:reasons.append('LIMIT_PAIR_NOT_VISIBLE_BY_OPEN_AFTER_SESSION_RESET')
    elif Decimal(prior[18]['price'])>=Decimal(prior[17]['price']):
        reasons.append('OPENING_LIMIT_PAIR_INVALID')
    if not prices['records'] or timestamp_ns(prices['records'][0]['bucket_start'])!=first:
        reasons.append('NO_OBSERVED_FIRST_BUCKET_NO_BACKFILLED_OPEN')
    bar=prices['session_ohlcv']
    padding=Decimal(str(tick_size))*5 if tick_size else None
    if padding is None:reasons.append('CONTRACT_TICK_UNKNOWN_FOR_LIMIT_SAFE_FILL')
    if known and bar and padding is not None and not Decimal(prior[18]['price'])+padding<Decimal(bar['open'])<Decimal(prior[17]['price'])-padding:
        reasons.append('OPEN_AT_OR_OUTSIDE_VISIBLE_LIMIT')
    # Conflicting metadata is not repaired by picking a convenient side.
    if any(timestamp_ns(x['capture_at'])<=first for x in conflicts):reasons.append('OPEN_LIMIT_VERSION_CONFLICT_REQUIRES_REVIEW')
    stop_reasons=[]
    if conflicts:stop_reasons.append('LIMIT_VERSION_CONFLICT_REQUIRES_REVIEW')
    if cohort['unexpected']:stop_reasons.append('INTRADAY_UNSCHEDULED_STATUS_PROXY_UNKNOWN')
    states=dict(prior);all_limits=[]
    for item in [*prior.values(),*events]:
        states[item['stat_type']]=item
        if set(states)=={17,18} and all(x['action']==1 and x['price'] is not None for x in states.values()):
            lo,hi=Decimal(states[18]['price']),Decimal(states[17]['price'])
            if lo>=hi:stop_reasons.append('INTRADAY_LIMIT_PAIR_INVALID')
            all_limits.append((lo,hi))
        else:stop_reasons.append('INTRADAY_LIMIT_STATE_UNKNOWN')
    if bar and all_limits and padding is not None and any(Decimal(bar['low'])<=lo+padding or Decimal(bar['high'])>=hi-padding for lo,hi in all_limits):
        stop_reasons.append('DAILY_RANGE_TOUCHES_A_LIMIT_PROXY_UNKNOWN')
    return dict(status='DATED_STATUS_AND_LIMITS_REPLAYED',tradable_open=not reasons,
       tradable_stop=not reasons and not stop_reasons,open_reasons=reasons,stop_reasons=sorted(set(stop_reasons)),
       opening_status=opening,opening_limits={str(k):v for k,v in prior.items()},limit_updates=events,
       conflicts=conflicts,unscheduled_events=cohort['unexpected'],
       execution_model='FROZEN_DAILY_OPEN_AND_STOP_PROXY_NOT_OBSERVED_QUOTES_OR_REAL_BROKER_FILLS',
       fill_limit_padding_quote_units=str(padding) if padding is not None else None,
       padding_basis='MAX_FROZEN_4_ADVERSE_TICKS_PLUS_LESS_THAN_1_TICK_ROUNDING_BOUND_NOT_NEW_SLIPPAGE',
       historical_customer_receipt=None)


def settlement_audit_scope(catalog,contract,identity,start,target):
    """Bound a negative revision assertion to actual requests and quality dates."""
    coverage=catalog.interval_coverage(contract,'statistics',start,target)
    if not coverage['request_complete']:
        raise ValueError('SETTLEMENT_AUDIT_RANGE_NOT_FULLY_REQUESTED')
    end=target;dates=[]
    for day in coverage['dates']:
        a=timestamp_ns(day['date']+'T00:00:00Z')
        if day['conditions'] and all(x=='available' for x in day['conditions']):
            dates.append(dict(**day,status='VENDOR_AVAILABLE'));continue
        # Saturday is outside the CME weekly trading schedule. The completed
        # vendor response is separately checked for any relevant messages;
        # this is explicitly a no-reported-message result, not an invented bar.
        absent_saturday=(not day['conditions'] and date.fromisoformat(day['date']).weekday()==5)
        messages=next(catalog.rows('statistics',identity=identity,after=a,before=a+DAY-1),None) if absent_saturday else None
        if absent_saturday and messages is None:
            dates.append(dict(**day,status='CLOSED_SATURDAY_NO_REPORTED_RELEVANT_MESSAGES_IN_COMPLETE_RESPONSE'));continue
        end=min(end,max(start,a));dates.append(dict(**day,status='AUDIT_STOPS_BEFORE_UNKNOWN_OR_DEGRADED_DATE'));break
    return end,dict(request_proof=coverage,quality_checked_dates=dates,effective_audit_end=iso_ns(end),
        claim='NO_OBSERVED_ECONOMIC_REVISION_ONLY_WITHIN_THIS_ACTUALLY_REQUESTED_PREFIX',
        later_unobserved_or_unqualified_versions='UNKNOWN_NOT_BACKFILLED')


def qualify_session(result,*,catalog,context,calendar,definition,registry_row,definition_issues):
    rules=load_rules(context['rules'],context['data_root'])
    contract=result['contract_id'];session=result['session'];identity=(result['publisher_id'],result['instrument_id'])
    if contract not in rules['scope']['contracts'] or not context['scope']['start']<=session<=rules['scope']['end']:
        raise ValueError('QUALIFICATION_OUTSIDE_FIXED_ENGINEERING_SLICE')
    if context['scope'] not in (dict(start='2024-07-01',end='2024-10-04'),dict(start='2024-06-17',end='2024-10-04')):
        raise ValueError('QUALIFICATION_SCOPE_NOT_FROZEN_ENGINEERING_SLICE')
    issues=[dict(layer='DEFINITION',code=x) for x in definition_issues]
    if result['definition']['units']['unit_comparison']!='MATCH':
        issues.append(dict(layer='UNITS',code='ACTUAL_VENDOR_FROZEN_REGISTRY_UNIT_MISMATCH'))
    cohort=calendar['cohort'];start=timestamp_ns(cohort['segments'][0][0]);end=timestamp_ns(cohort['segments'][-1][1])
    if not cohort['coverage_qualified']:issues.append(dict(layer='CALENDAR',code='FULL_STATUS_COHORT_COVERAGE_NOT_QUALIFIED'))
    study_start='2024-06-16T22:00:00Z' if context['scope']['start']=='2024-06-17' else '2024-06-30T22:00:00Z'
    preexisting=next((x for x in catalog.rows('ohlcv-1h',identity=identity,before=timestamp_ns(study_start)-HOUR_NS)
                     if x['volume'] and x['volume']>0),None)
    boundary=fixed_sp500_boundaries(contract,definition,preexisting,study_start=study_start,rules=context['rules'],eligible_from=context['scope']['start'])
    boundary['evidence']=result['boundaries']['evidence'];boundary['issues']=[]
    boundary['research_qualified']=True;boundary['live_execution_eligible']=False
    result['boundaries']=boundary
    coverage=[]
    for a,b in cohort['segments']:
        for schema in ('status','statistics'):
            proof=catalog.interval_coverage(contract,schema,timestamp_ns(a),timestamp_ns(b))
            coverage.append(dict(schema=schema,**proof))
            if proof['status']!='COMPLETE_REQUEST_AVAILABLE_DATASET':
                issues.append(dict(layer='COVERAGE',code=schema+':'+proof['status']))
    # Every selected price bucket, not just missing ones, must have complete
    # source requests and available dataset dates for this identity/interval.
    for record in result['prices']['records']:
        a=timestamp_ns(record['bucket_start']);width=HOUR_NS if record['schema']=='ohlcv-1h' else 60*NANO
        proof=catalog.interval_coverage(contract,record['schema'],a,a+width)
        if proof['status']!='COMPLETE_REQUEST_AVAILABLE_DATASET':
            issues.append(dict(layer='PRICES',code=proof['status']))
    if result['prices']['session_ohlcv'] is None:issues.append(dict(layer='PRICES',code='INCOMPLETE_OR_NO_OBSERVED_PRICES'))
    temporal=result['temporal']
    if temporal is None or timestamp_ns(temporal['available_at'])<timestamp_ns(cohort['calendar_confirmed_at']):
        issues.append(dict(layer='CAUSALITY',code='STATUS_CONFIRMATION_NOT_IN_INTERNAL_CLOCK'))
    if timestamp_ns(result['decision_at'])<max(end,timestamp_ns(temporal['available_at']) if temporal else end):
        issues.append(dict(layer='CAUSALITY',code='INTERNAL_LOGICAL_CALCULATION_AFTER_DECISION_CUTOFF'))
    reset=timestamp_ns(cohort['reset_capture_at'])
    limit_coverage=catalog.interval_coverage(contract,'statistics',reset,start+1)
    if limit_coverage['status']!='COMPLETE_REQUEST_AVAILABLE_DATASET':
        issues.append(dict(layer='EXECUTION',code='LIMITS_PREOPEN_REQUEST_OR_DATASET_COVERAGE_UNKNOWN'))
    audit_end,audit_scope=settlement_audit_scope(catalog,contract,identity,start,timestamp_ns(result['decision_at']))
    first,settlement_audit=first_clearing_final(catalog,identity,session,audit_end-1)
    settlement_audit['coverage_scope']=audit_scope
    settled=result['settlement'];settled['account_selection_audit']=settlement_audit
    if settlement_audit['status']!='FIRST_FINAL_WITHOUT_LATER_ECONOMIC_REVISION':
        issues.append(dict(layer='SETTLEMENT',code=settlement_audit['status']))
    if first is not None:
        final_proof=catalog.interval_coverage(contract,'statistics',first['ts_recv_ns'],first['ts_recv_ns']+1)
        settlement_audit['selected_final_coverage']=final_proof
        if final_proof['status']!='COMPLETE_REQUEST_AVAILABLE_DATASET':
            issues.append(dict(layer='SETTLEMENT',code='SELECTED_FINAL_CAPTURE_DATA_QUALITY_UNKNOWN'))
        reference_at=datetime.combine(date.fromisoformat(session),time.fromisoformat(
             rules['special_settlement_times'].get(session,rules['ordinary_settlement_time_chicago'])),ZONE)
        ref=timestamp_ns(reference_at)
        if session in rules['no_new_settlement_dates'] or not start<=ref<=end or ref>first['ts_recv_ns']:
            issues.append(dict(layer='SETTLEMENT',code='SETTLEMENT_RULE_REFERENCE_OUTSIDE_ACTUAL_SESSION'))
        settled.update(settlement=first['price'],capture_available_at=first['ts_recv'],
            settlement_pricing_reference_at=iso_ns(ref),selected_account_record=reference(first),
            reference_time_source=context['rules'],reference_time_role='EXCHANGE_PRICING_WINDOW_END_NOT_PUBLICATION')
    result['execution_status']=execution_evidence(catalog,identity,cohort,result['prices'],tick_size=registry_row['tick_in_quote_units'])
    result['execution_status']['preopen_limit_coverage']=limit_coverage
    result['qualification_issues']=issues
    result['research_qualified']=not issues
    result['status']='QUALIFIED_CONTINUOUS_ENGINEERING_INPUT' if not issues else 'DATED_RAW_SESSION_CANDIDATE_RESEARCH_BLOCKED'
    result['qualification_proof']=dict(context_sha256=canonical_hash(context),dated_coverage=coverage,
        contract_boundary_sha256=canonical_hash(boundary),scope='FIXED_SP500_ENGINEERING_SLICE_ONLY',
        first_trade_date_status='UNKNOWN_NOT_REPLACED_BY_OBSERVED_START',
        next_session_role='OBSERVED_CALENDAR_CONTINUITY_AUDIT_NOT_VENDOR_PUBLICATION',
        review_method='RECOMPUTED_RAW_RECORDS_RULES_AND_RECEIPTS_NO_CALLER_APPROVAL_FLAG')


HOUR_NS=3600*NANO


def qualified_contract(catalog,context,contract,registry_row):
    rules=load_rules(context['rules'],context['data_root'])
    study=timestamp_ns('2024-06-16T22:00:00Z' if context['scope']['start']=='2024-06-17' else '2024-06-30T22:00:00Z');chosen=None
    for row in catalog.rows('definition',before=study):
        if row['raw_symbol']==contract:chosen=row
    if chosen is None or chosen['security_update_action'] not in ('A','M'):
        raise ValueError('CONTRACT_NOT_OBSERVED_ACTIVE_BEFORE_FIXED_SLICE')
    if compare_definition(chosen,registry_row)['unit_comparison']!='MATCH':
        raise ValueError('QUALIFIED_CONTRACT_UNITS_DIFFER')
    identity=(chosen['publisher_id'],chosen['instrument_id'])
    existence=next((x for x in catalog.rows('ohlcv-1h',identity=identity,before=study-HOUR_NS)
                   if x['volume'] and x['volume']>0),None)
    result=fixed_sp500_boundaries(contract,chosen,existence,study_start=iso_ns(study),rules=context['rules'],eligible_from=context['scope']['start'])
    result.update(contract_id=contract,identity=list(identity),root=registry_row['root'],market=registry_row['market'])
    return result
