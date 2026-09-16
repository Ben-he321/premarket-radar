"""GLBX settlement versions, separated by economic purpose, not arrival order.

CME SettlPriceType bit 2 distinguishes display/trading precision from clearing
precision. Only final actual EOD clearing values may mark the research account.
This state machine consumes an already bounded capture-time prefix; it never
manufactures supplier publication or historical customer receipt timestamps.
"""
from __future__ import annotations


class SettlementState:
    def __init__(self):
        self.chains = {}
        self.updates = 0

    def add(self, row):
        flags = row['settlement_flags']
        # Final/preliminary and actual/theoretical are replacement validity,
        # not separate keys that could resurrect a stale earlier final value.
        key = bool(flags['trading_tick']), bool(flags['intraday'])
        at = row['ts_recv_ns']
        payload = (row['price'], row['update_action'], row['stat_flags'], row['ts_ref'])
        old = self.chains.get(key)
        if old is not None and at < old['at']:
            raise ValueError('SETTLEMENT_CAPTURE_ORDER_REVERSED')
        if old is not None and at == old['at']:
            # Identical economic observations are idempotent. Sequence/channel
            # across files does not establish precedence for conflicting values.
            if payload != old['payload']:
                old['ambiguous'] = True
        else:
            self.chains[key] = dict(at=at, payload=payload, row=row, ambiguous=False)
        self.updates += 1

    def account_candidate(self):
        chain = self.chains.get((False, False))
        if chain is None:
            return None, ('ONLY_TRADING_PRECISION_NO_CLEARING_SETTLEMENT'
                          if (True, False) in self.chains else
                          'NO_SETTLEMENT_MESSAGE_VISIBLE_AT_CUTOFF')
        row, flags = chain['row'], chain['row']['settlement_flags']
        if chain['ambiguous']:
            return None, 'CONFLICTING_CLEARING_SETTLEMENT_SAME_CAPTURE'
        if row['update_action'] == 2:
            return None, 'LATEST_VISIBLE_VERSION_DELETED'
        if row['price'] is None or not flags['final'] or not flags['actual'] or flags['unknown_bits']:
            return None, 'LATEST_VISIBLE_VERSION_NOT_FINAL_ACTUAL_EOD'
        if row['ts_event_ns'] is None or row['ts_event_ns'] > row['ts_recv_ns']:
            return None, 'SETTLEMENT_EVENT_CAPTURE_ORDER_UNKNOWN'
        return row, 'FINAL_ACTUAL_EOD_CLEARING_CAPTURE_PREFIX_CANDIDATE'

    @property
    def clearing_chain(self):
        return self.chains.get((False, False))
