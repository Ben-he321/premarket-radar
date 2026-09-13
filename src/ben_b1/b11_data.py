"""B1.1 fixed engineering samples and isolated, resumable real SIP inputs.

No strategy returns are computed here. Historical receive times stay UNKNOWN.
Existing B1 objects are referenced/read only, never modified.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import os
import time
import requests
import pandas as pd
from src.ben_b1.data_probe import ProbeMarket, read, write, utc, digest, schedule, rth_aggregate, load_universe, OLD

OLD_OUT = Path(r'C:\Users\benhe\BenAITradingData\ben-b1-research-20260913')
OUT = Path(r'C:\Users\benhe\BenAITradingData\ben-b1-1-replay-20260914\data')
QUOTE_SOURCE = 'https://docs.alpaca.markets/us/v1.1/changelog/marketdata-bid-and-ask-size-display-change'
START = '2026-01-02'
END = '2026-09-11'
WARMUP = '2025-06-01'


def hashfile(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def attach_page_receipts(frame, receipt):
    """Individual real download receipts, never a made-up historical receipt."""
    if frame.empty:
        return frame
    page_times = {}
    for path in sorted(Path(receipt['path']).glob('page_[0-9][0-9][0-9][0-9][0-9].json')):
        rp = path.with_name(path.stem + '_receipt.json')
        if not rp.exists():
            continue
        rr = read(rp)
        if hashfile(path) != rr['sha256']:
            raise ValueError('CACHE_PAGE_HASH_MISMATCH')
        payload = read(path).get(receipt['kind'], {})
        for data in payload.values():
            for item in data:
                page_times[item['t']] = rr.get('source_received_at', 'UNKNOWN')
    field = 'timestamp_original' if 'timestamp_original' in frame else 't'
    frame = frame.copy()
    frame['source_received_at'] = frame[field].map(page_times).fillna('UNKNOWN')
    frame['input_snapshot_completed_at'] = receipt['last_source_received_at']
    frame['historical_network_received_at'] = 'UNKNOWN'
    return frame


def freeze(output=OUT):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    source = OLD_OUT / 'coarse/all_candidate_events.csv'
    scopes = pd.read_csv(OLD_OUT / 'UNIVERSE_POLICY.csv')
    events = pd.read_csv(source)
    selected = events[events.trade_date.between(START, END) & events.coarse_space_allowed.eq(True)].copy()
    selected['scope_policy'] = selected.symbol.map(scopes.set_index('symbol').scope_policy.to_dict())
    selected = selected.sort_values(['trade_date', 'identity_sort_hash', 'symbol'], kind='stable').head(20)
    path = output / 'FIRST20_FROZEN.csv'
    if path.exists():
        existing = pd.read_csv(path)
        if existing[['symbol', 'trade_date', 'identity_sort_hash']].to_dict('records') != selected[['symbol', 'trade_date', 'identity_sort_hash']].to_dict('records'):
            raise ValueError('FIRST20_FROZEN_SELECTION_MISMATCH')
    else:
        selected.to_csv(path, index=False, encoding='utf-8-sig')
    protocol = {'experiment_start': START, 'experiment_end': END,
                'warmup_request_start': WARMUP, 'required_valid_rth_sessions': 100,
                'preferred_sessions': 300, 'warmup_target_note': 'Fixed 2025-06 warmup offers >100 sessions before Jan 2026; 300 is preferred, not required.',
                'selection': 'First20 all66 coarse_space_allowed, ascending trade_date then EXISTING identity_sort_hash then symbol; excluded retained diagnostics only',
                'selection_source': str(source), 'selection_source_sha256': hashfile(source),
                'first20_sha256': hashfile(path), 'selected_before_earnings_quote_or_return_outcomes': True,
                'quote_size_unit': 'shares', 'quote_effective_start': '2025-11-03', 'quote_source': QUOTE_SOURCE,
                'old_historical_quote_rewrite_claim': 'UNKNOWN_NOT_USED',
                'quote_event_identifier': 'SHA256(symbol plus entire original quote record); consumption persists per quote identity and side',
                'quote_displayed_size_is_not_queue_position_or_actual_fill_proof': True}
    protocol_path = output / 'DATA_PROTOCOL.json'
    if protocol_path.exists() and read(protocol_path) != protocol:
        raise ValueError('DATA_PROTOCOL_ALREADY_FROZEN')
    write(protocol_path, protocol)
    scopes.to_csv(output / 'ALL66_SCOPE_VERSION.csv', index=False, encoding='utf-8-sig')
    return selected


class B11Market(ProbeMarket):
    def __init__(self, output=OUT):
        super().__init__(output)
        old_state = OLD_OUT / 'data_probe/PROBE_STATE.json'
        self.previous = read(old_state).get('requests', []) if old_state.exists() else []
        self.state.update({'sdk_http_attempt_count': None, 'sdk_hidden_retry_count': None,
                           'new_successful_http_pages': self.state.get('api_pages', 0),
                           'cache_reuse_records': self.state.get('cache_reuse_records', []),
                           'market_feed': 'sip', 'broker_calls': 0, 'paid_model_calls': 0})
        self.save()

    def acquire(self, symbol, start, end, kind='bars', adjustment='raw', timeframe='1Min', scope='B11_FIXED', **kwargs):
        if kind == 'quotes' and pd.Timestamp(start) < pd.Timestamp('2025-11-03T00:00:00Z'):
            raise ValueError('PRE_CHANGE_QUOTE_SIZE_UNVERIFIED_NOT_ALLOWED')
        # Match existing complete superset requests without writing to any old object.
        if kind == 'bars':
            for prior in self.previous:
                params = prior.get('params', {})
                if prior.get('symbol') != symbol or prior.get('kind') != kind or not prior.get('complete'):
                    continue
                if params.get('adjustment') != adjustment or params.get('timeframe') != timeframe:
                    continue
                if pd.Timestamp(params['start']) > pd.Timestamp(start) or pd.Timestamp(params['end']) < pd.Timestamp(end):
                    continue
                data_path = Path(prior['path']) / 'data.parquet'
                if not data_path.exists():
                    continue
                frame = pd.read_parquet(data_path)
                frame = frame[frame.timestamp.between(pd.Timestamp(start), pd.Timestamp(end))].copy()
                record = {**prior, 'old_cache_reused_read_only': True, 'old_input_sha256': hashfile(data_path),
                          'requested_start': str(start), 'requested_end': str(end), 'rows': len(frame),
                          'scope_b11': scope, 'historical_network_received_at': 'UNKNOWN'}
                self.state['cache_reuse_records'].append(record)
                self.save()
                return attach_page_receipts(frame, record), record
        frame, receipt = super().acquire(symbol, start, end, kind, adjustment, timeframe, scope, **kwargs)
        frame = attach_page_receipts(frame, receipt)
        receipt.update({'size_unit': 'shares' if kind == 'quotes' else 'VOLUME_SHARES',
                        'historical_network_received_at': 'UNKNOWN',
                        'event_timestamp_precision': 'ORIGINAL_VENDOR_TIMESTAMP',
                        'old_quote_normalization_verified': False})
        if kind == 'quotes' and len(frame):
            original_columns = [x for x in frame if x not in {'source_received_at', 'input_snapshot_completed_at', 'historical_network_received_at'}]
            frame['quote_id'] = [digest({'symbol': symbol, 'original_record': item}) for item in frame[original_columns].to_dict('records')]
            frame['size_unit'] = 'shares'
            frame.to_parquet(Path(receipt['path']) / 'data.parquet', index=False)
        write(Path(receipt['path']) / 'summary.json', receipt)
        self.state['requests'] = [r for r in self.state['requests'] if r.get('cache_key') != receipt['cache_key']] + [receipt]
        self.state['new_successful_http_pages'] = self.state['api_pages']
        self.save()
        return frame, receipt

    def corporate_actions(self, symbols):
        params = {'symbols': ','.join(symbols), 'start': '2025-01-01', 'end': END,
                  'limit': 1000, 'sort': 'asc', 'data_quality': 'all'}
        dest = self.output / 'corporate_actions.json'
        if dest.exists():
            return read(dest)
        rows, attempts, token, receipts = {}, 0, None, []
        status = 'UNKNOWN'
        for page in range(100):
            query = dict(params)
            if token:
                query['page_token'] = token
            self.sdk.shared_limiter.acquire()
            requested = utc()
            try:
                attempts += 1
                response = requests.get('https://data.alpaca.markets/v1/corporate-actions', params=query,
                                        headers={'APCA-API-KEY-ID': self.config.api_key,
                                                 'APCA-API-SECRET-KEY': self.config.secret_key}, timeout=(10, 45))
                receipt = {'request_started_at': requested, 'source_received_at': utc(), 'http_status': response.status_code}
                receipts.append(receipt)
                if not response.ok:
                    status = 'AUTH_FAILED' if response.status_code == 401 else 'ENDPOINT_ENTITLEMENT_DENIED' if response.status_code == 403 else 'REQUEST_FAILED'
                    break
                value = response.json()
                for key, records in (value.get('corporate_actions') or {}).items():
                    rows.setdefault(key, []).extend(records)
                token = value.get('next_page_token')
                if not token:
                    status = 'ACCESS_OK' if any(rows.values()) else 'EMPTY_RESPONSE'
                    break
            except (requests.RequestException, ValueError):
                status = 'NETWORK_OR_RESPONSE_ERROR'
                break
        result = {'parameters': params, 'status': status, 'corporate_actions': rows, 'receipts': receipts,
                  'request_attempt_count': attempts, 'hidden_retry_count': 0,
                  'creation_time_or_historical_available_at': 'UNKNOWN_PROVIDER_MAKES_NO_CREATION_TIME_GUARANTEE',
                  'basis': 'VENDOR_STANDARD_HISTORICAL_ACTIONS_NOT_INDEPENDENT_PIT'}
        write(dest, result)
        return result


def samples(output=OUT):
    output = Path(output)
    first = freeze(output)
    market = B11Market(output)
    records = []
    for rank, event in enumerate(first.to_dict('records'), 1):
        symbol, day = event['symbol'], event['trade_date']
        session = schedule(day, day).iloc[0]
        close = session.market_close
        windows = [('ENTRY_1605_1615', close + pd.Timedelta(minutes=5, seconds=-5), close + pd.Timedelta(minutes=15)),
                   ('DECISION_MINUS10', close - pd.Timedelta(minutes=10, seconds=5), close - pd.Timedelta(minutes=10))]
        later = schedule((pd.Timestamp(day) + pd.Timedelta(days=1)).date(), (pd.Timestamp(day) + pd.Timedelta(days=7)).date()).iloc[0]
        windows += [('NEXT_OPEN_5MIN', later.market_open, later.market_open + pd.Timedelta(minutes=5)),
                    ('NEXT_MINUS10', later.market_close - pd.Timedelta(minutes=10, seconds=5), later.market_close - pd.Timedelta(minutes=10))]
        row = {'rank': rank, 'symbol': symbol, 'trade_date': day, 'scope_policy': event['scope_policy'],
               'excluded_never_ordered': event['scope_policy'] != 'KEEP', 'quote_windows': []}
        for label, begin, finish in windows:
            frame, receipt = market.acquire(symbol, begin, finish, kind='quotes', scope='B11_FIRST20_' + label)
            raw_times = pd.to_datetime(frame.t, utc=True) if len(frame) else pd.Series(dtype='datetime64[ns, UTC]')
            row['quote_windows'].append({'kind': label, 'start': begin.isoformat(), 'end': finish.isoformat(),
                                        'receipt': receipt, 'first_at': raw_times.min().isoformat() if len(frame) else None,
                                        'last_at': raw_times.max().isoformat() if len(frame) else None,
                                        'distinct_quote_ids': frame.quote_id.nunique() if len(frame) else 0})
        auction, ar = market.acquire(symbol, close - pd.Timedelta(seconds=2), close + pd.Timedelta(seconds=5),
                                     kind='trades', scope='B11_FIRST20_CLOSING_AUCTION')
        row['closing_auction'] = ar
        row['auction_condition6_prices'] = sorted(set(auction[auction.c.apply(lambda value: '6' in value)].p)) if len(auction) else []
        records.append(row)
        write(output / 'FIRST20_INPUTS.json', records)
        print(json.dumps({'phase': 'FIRST20', 'rank': rank, 'symbol': symbol, 'day': day,
                          'quote_rows': sum(x['receipt']['rows'] for x in row['quote_windows'])}), flush=True)
    market.state['status'] = 'FIRST20_QUOTES_COMPLETE'
    market.save()
    return records


def histories(output=OUT):
    output = Path(output)
    first = freeze(output)
    market = B11Market(output)
    symbols = list(dict.fromkeys(first[first.scope_policy.eq('KEEP')].symbol.tolist()))
    records = []
    market.corporate_actions(symbols)
    for symbol in symbols:
        row = {'symbol': symbol, 'start': WARMUP, 'end': END, 'daily': {}}
        for adjustment in ['raw', 'split', 'all']:
            frame, receipt = market.acquire(symbol, WARMUP + 'T00:00:00Z', '2026-09-12T03:59:59Z',
                                            timeframe='1Day', adjustment=adjustment, scope='B11_FIXED_DAILY')
            path = output / (symbol + '_daily_' + adjustment + '.parquet')
            frame.to_parquet(path, index=False)
            row['daily'][adjustment] = {'path': str(path), 'sha256': hashfile(path), 'receipt': receipt}
        frame, receipt = market.acquire(symbol, WARMUP + 'T00:00:00Z', '2026-09-12T03:59:59Z', scope='B11_WARMUP_AND_FIXED_CONTINUOUS_MINUTES')
        path = output / (symbol + '_minutes_raw.parquet')
        frame.to_parquet(path, index=False)
        row['minutes'] = {'path': str(path), 'sha256': hashfile(path), 'receipt': receipt}
        rth = rth_aggregate(frame, WARMUP, END)
        daily = pd.read_parquet(output / (symbol + '_daily_raw.parquet')).set_index('trade_date')
        # Official daily close, separately marked from RTH last-minute price. Daily high/low not used for RTH.
        if len(rth):
            rth['last_minute_close'] = rth['close']
            rth['vendor_official_close'] = rth.trade_date.map(daily.close.to_dict())
            rth['close'] = rth['vendor_official_close']
            # The 16:00 closing auction is outside the 15:59 minute, but belongs
            # in regular-session price range. Preserve the observed minute range.
            rth['minute_only_high'] = rth['high']
            rth['minute_only_low'] = rth['low']
            rth['high'] = rth[['high', 'close']].max(axis=1)
            rth['low'] = rth[['low', 'close']].min(axis=1)
            rth['regular_session_verified'] = True
            rth['action_units_verified'] = True
            rth['action_unit_basis'] = 'VENDOR_RAW_SAME_SESSION_UNITS_SPLIT_ACTIONS_SEPARATE'
            rth['volume_basis'] = 'SUM_OBSERVED_09_30_TO_BEFORE_CALENDAR_CLOSE_MINUTES;CLOSING_AUCTION_EXCLUDED_IF_TIMESTAMP_AT_OR_AFTER_CLOSE'
            rth['auction_inclusive_volume_verified'] = False
            rth['independent_all_warmup_auction_validation'] = 'NOT_INDEPENDENTLY_CHECKED_EACH_POINT'
            minute_receipts = frame.groupby('trade_date').source_received_at.max().to_dict()
            daily_receipts = daily.source_received_at.to_dict()
            rth['source_received_at'] = [max(minute_receipts.get(day, 'UNKNOWN'), daily_receipts.get(day, 'UNKNOWN')) for day in rth.trade_date]
            rth['input_snapshot_completed_at'] = receipt['last_source_received_at']
            rth['historical_network_received_at'] = 'UNKNOWN'
            rth.to_parquet(output / (symbol + '_rth.parquet'), index=False)
        row['rth'] = {'path': str(output / (symbol + '_rth.parquet')), 'rows': len(rth),
                      'sha256': hashfile(output / (symbol + '_rth.parquet')) if len(rth) else None,
                      'pre_start_valid_sessions': int((rth.trade_date < START).sum()) if len(rth) else 0,
                      'final_session': rth.trade_date.max() if len(rth) else None}
        records.append(row)
        write(output / 'HISTORY_INPUTS.json', records)
        print(json.dumps({'phase': 'HISTORIES', 'symbol': symbol, 'daily_rows': len(daily),
                          'minutes': len(frame), 'rth_sessions': len(rth)}), flush=True)
    market.state['status'] = 'FIXED_HISTORY_INPUTS_COMPLETE'
    market.save()
    return records


def supplement_ranking_volume(output=OUT, request_cap=500):
    """Only predetermined first20's prior20 sessions; no return-based data choice."""
    output = Path(output)
    first = freeze(output)
    calendar = schedule('2025-10-01', END)
    sessions = [str(x.date()) for x in calendar.index]
    required = set()
    sample_windows = []
    for event in first[first.scope_policy.eq('KEEP')].to_dict('records'):
        at = sessions.index(event['trade_date'])
        dates = sessions[max(0, at - 20):at]
        required.update((event['symbol'], day) for day in dates)
        sample_windows.append({'symbol': event['symbol'], 'trade_date': event['trade_date'], 'prior20_sessions': dates})
    frozen = {'selection': 'Union of prior20 NYSE sessions before each unchanged FIRST20 KEEP event',
              'required_symbol_dates': [list(x) for x in sorted(required)], 'request_cap': request_cap,
              'not_return_selected': True, 'source_first20_sha256': hashfile(output / 'FIRST20_FROZEN.csv')}
    frozen_path = output / 'RANKING_VOLUME_REQUEST_FREEZE.json'
    if frozen_path.exists() and read(frozen_path) != frozen:
        raise ValueError('RANKING_VOLUME_REQUEST_ALREADY_FROZEN')
    write(frozen_path, frozen)
    if len(required) > request_cap:
        raise ValueError('FIXED_VOLUME_REQUEST_CAP_EXCEEDED')
    market = B11Market(output)
    rows = []
    rth = {symbol: pd.read_parquet(output / (symbol + '_rth.parquet')).set_index('trade_date') for symbol, _ in required}
    for index, (symbol, day) in enumerate(sorted(required), 1):
        s = calendar.loc[pd.Timestamp(day)]
        close = s.market_close
        frame, receipt = market.acquire(symbol, close - pd.Timedelta(seconds=2), close + pd.Timedelta(seconds=5),
                                        kind='trades', scope='B11_FIRST20_CLOSING_AUCTION')
        official_close = float(rth[symbol].loc[day, 'close']) if day in rth[symbol].index else None
        minute_volume = float(rth[symbol].loc[day, 'volume']) if day in rth[symbol].index else None
        auction = frame[frame.c.apply(lambda x: '6' in x)].drop_duplicates(['x', 'i']) if len(frame) else frame
        after = auction[pd.to_datetime(auction.t, utc=True) >= close] if len(auction) else auction
        matches = bool(len(auction) and official_close is not None and any(abs(float(p) - official_close) < 1e-8 for p in auction.p))
        known = bool(receipt['complete'] and len(auction) and matches and minute_volume is not None)
        extra = float(after.s.sum()) if known else None
        # An actual auction before the minute boundary is already counted there.
        total = minute_volume + extra if known else None
        rows.append({'symbol': symbol, 'trade_date': day, 'official_close': official_close,
                     'rth_minute_volume': minute_volume, 'observed_condition6_records': len(auction),
                     'auction_matches_official_close': matches,
                     'observed_auction_quantity_after_calendar_close': extra,
                     'rth_plus_observed_auction_volume': total,
                     'ranking_volume_status': 'OBSERVED_RTH_MINUTES_PLUS_MATCHING_CLOSING_AUCTION' if known else 'UNKNOWN_AUCTION_QUANTITY_OR_PRICE_MATCH',
                     'late_auction_prints_outside_fixed_window': 'NOT_INDEPENDENTLY_AUDITED',
                     'trade_cache_path': receipt['path'], 'trade_cache_key': receipt['cache_key'],
                     'source_received_at': receipt['last_source_received_at'], 'historical_network_received_at': 'UNKNOWN'})
        pd.DataFrame(rows).to_csv(output / 'RANKING_VOLUME_PRIOR20.csv', index=False, encoding='utf-8-sig')
        if index % 20 == 0 or index == len(required):
            print(json.dumps({'phase': 'RANKING_VOLUME', 'done': index, 'total': len(required)}), flush=True)
    by_key = {(x['symbol'], x['trade_date']): x for x in rows}
    windows = []
    for sample in sample_windows:
        parts = [by_key[(sample['symbol'], day)] for day in sample['prior20_sessions']]
        unknown = [x['trade_date'] for x in parts if x['rth_plus_observed_auction_volume'] is None]
        windows.append({**sample, 'known_sessions': len(parts) - len(unknown), 'unknown_sessions': unknown,
                        'prior20_dollar_volume': sum(x['official_close'] * x['rth_plus_observed_auction_volume'] for x in parts) / 20 if len(parts) == 20 and not unknown else None,
                        'status': 'KNOWN_VENDOR_PLUS_OBSERVED_AUCTION' if len(parts) == 20 and not unknown else 'RANKING_VOLUME_UNKNOWN'})
    write(output / 'FIRST20_DOLLAR_VOLUME_WINDOWS.json', windows)
    market.state['status'] = 'FIXED_RANKING_VOLUME_COMPLETE'
    market.save()
    return windows


def extend_unknown_auctions(output=OUT):
    """A single fixed +60-second retrieval for incomplete predeclared inputs."""
    import shutil
    output = Path(output)
    source = output / 'RANKING_VOLUME_PRIOR20.csv'
    old = output / 'RANKING_VOLUME_PRIOR20_BEFORE_60SEC_EXTENSION.csv'
    if not old.exists():
        shutil.copyfile(source, old)
    frame = pd.read_csv(source)
    rows = frame.to_dict('records')
    unknown = [row for row in rows if pd.isna(row['rth_plus_observed_auction_volume'])]
    if len(frame) + len(unknown) > 500:
        raise ValueError('FIXED_TOTAL_AUCTION_REQUEST_CAP_EXCEEDED')
    market = B11Market(output)
    for row in unknown:
        symbol, day = row['symbol'], row['trade_date']
        close = schedule(day, day).iloc[0].market_close
        trades, receipt = market.acquire(symbol, close - pd.Timedelta(seconds=2), close + pd.Timedelta(seconds=60),
                                         kind='trades', scope='B11_FIXED_PRIOR20_SINGLE_60SECOND_EXTENSION')
        auction = trades[trades.c.apply(lambda x: '6' in x)].drop_duplicates(['x', 'i']) if len(trades) else trades
        after = auction[pd.to_datetime(auction.t, utc=True) >= close] if len(auction) else auction
        matches = bool(len(auction) and any(abs(float(p) - row['official_close']) < 1e-8 for p in auction.p))
        known = bool(receipt['complete'] and matches and pd.notna(row['rth_minute_volume']))
        row.update({'observed_condition6_records': len(auction), 'auction_matches_official_close': matches,
                    'observed_auction_quantity_after_calendar_close': float(after.s.sum()) if known else None,
                    'rth_plus_observed_auction_volume': row['rth_minute_volume'] + float(after.s.sum()) if known else None,
                    'ranking_volume_status': 'OBSERVED_RTH_MINUTES_PLUS_MATCHING_CLOSING_AUCTION_60SEC_WINDOW' if known else 'UNKNOWN_AUCTION_QUANTITY_OR_PRICE_MATCH',
                    'trade_cache_path': receipt['path'], 'trade_cache_key': receipt['cache_key'],
                    'source_received_at': receipt['last_source_received_at'],
                    'single_window_extension_seconds': 60})
        pd.DataFrame(rows).to_csv(source, index=False, encoding='utf-8-sig')
    by_key = {(x['symbol'], x['trade_date']): x for x in rows}
    windows = read(output / 'FIRST20_DOLLAR_VOLUME_WINDOWS.json')
    for sample in windows:
        parts = [by_key[(sample['symbol'], day)] for day in sample['prior20_sessions']]
        missing = [x['trade_date'] for x in parts if pd.isna(x['rth_plus_observed_auction_volume'])]
        sample.update({'known_sessions': len(parts) - len(missing), 'unknown_sessions': missing,
                       'prior20_dollar_volume': sum(x['official_close'] * x['rth_plus_observed_auction_volume'] for x in parts) / 20 if len(parts) == 20 and not missing else None,
                       'status': 'KNOWN_VENDOR_PLUS_OBSERVED_AUCTION' if len(parts) == 20 and not missing else 'RANKING_VOLUME_UNKNOWN'})
    write(output / 'FIRST20_DOLLAR_VOLUME_WINDOWS.json', windows)
    market.state['status'] = 'FIXED_RANKING_VOLUME_EXTENDED_COMPLETE'
    market.save()
    return windows


def acquire_exit_window(symbol, trigger_at, trace_path, sample_id, output=OUT, *, start_at=None, end_at=None):
    """An actual engine pending-exit time determines this bounded input request.

    These objects and manifest are separate from the original pinned inputs.
    No entry is removed using its future path, and old runs remain reviewable.
    """
    output = Path(output)
    folder = output / 'exit_quote_supplement_v1'
    market = B11Market(folder)
    trigger = pd.Timestamp(trigger_at)
    start = pd.Timestamp(start_at) if start_at is not None else trigger - pd.Timedelta(minutes=1)
    end = pd.Timestamp(end_at) if end_at is not None else trigger + pd.Timedelta(minutes=5)
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise ValueError('INVALID_EXIT_PATH_WINDOW')
    quote_frame, receipt = market.acquire(symbol, start, end, kind='quotes', scope='B11_DETERMINISTIC_ENGINE_PENDING_EXIT')
    dest = Path(receipt['path']) / 'data.parquet'
    result = {'symbol': symbol, 'sample_id': sample_id, 'trigger_at': trigger.isoformat(),
              'request_start': start.isoformat(), 'request_end': end.isoformat(),
              'selection_basis': 'ACTUAL_NATURAL_CAMPAIGN_RTH_PATH_REQUEST_FROZEN_BEFORE_NEW_QUOTES_NOT_RETURN_SELECTED' if start_at is not None else 'FIRST_ACTUAL_ENGINE_PENDING_EXIT_TRIGGER_MINUS1_PLUS5_MINUTES_NOT_RETURN_SELECTED',
              'origin_trace': str(trace_path), 'origin_trace_sha256': hashfile(trace_path),
              'receipt': receipt, 'path': str(dest) if dest.exists() else None,
              'sha256': hashfile(dest) if dest.exists() else None,
              'rows': len(quote_frame), 'original_inputs_modified': False,
              'earlier_intraday_quotes_outside_observed_windows': 'UNKNOWN_NOT_BACKFILLED_AS_QUOTES',
              'source_layer': 'REAL_HISTORICAL_SIP_L1_MODEL_EXECUTION_NOT_LIVE_FILL_PROOF'}
    manifest_path = output.parent / 'EXIT_QUOTE_INPUTS.json'
    entries = read(manifest_path) if manifest_path.exists() else []
    identity = (symbol, start.isoformat(), end.isoformat(), str(trace_path))
    if not any((x['symbol'], x['request_start'], x['request_end'], x['origin_trace']) == identity for x in entries):
        entries.append(result)
        write(manifest_path, entries)
    market.state['status'] = 'DETERMINISTIC_EXIT_QUOTES_COMPLETE'
    market.save()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default=str(OUT))
    parser.add_argument('--freeze', action='store_true')
    parser.add_argument('--samples', action='store_true')
    parser.add_argument('--histories', action='store_true')
    parser.add_argument('--ranking-volume', action='store_true')
    parser.add_argument('--extend-unknown-auctions', action='store_true')
    args = parser.parse_args()
    if args.freeze:
        freeze(args.output)
    if args.samples:
        samples(args.output)
    if args.histories:
        histories(args.output)
    if args.ranking_volume:
        supplement_ranking_volume(args.output)
    if args.extend_unknown_auctions:
        extend_unknown_auctions(args.output)
