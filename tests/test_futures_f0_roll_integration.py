"""Temporary artificial integration inputs; never actual futures research.

Exercise the production decision producer through the engine's batch/event
consumer. The decision function rejects is_mock=True, so its minimal input
objects have the real-input field shape solely inside this engineering test.
All engine bars are explicitly mock, allow_mock=True labels the run MOCK, and
the on-disk fixture declaration/result remain in pytest's temporary directory.
"""
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import json

import pytest

from src.futures_f0.engine import FuturesEngine
from src.futures_f0.model import ContractSpec, EngineConfig, MarketDay, SessionBar
from src.futures_f0.rolls import decide_roll
from src.futures_f0.runtime import digest


START = date(2022, 1, 1)
ROLL_SESSION = START + timedelta(days=58)


def contract(contract_id, *, old, boundary):
    return ContractSpec(contract_id, 'SP500', 'MES', 5, 0.25, date(2021,1,1),
        date(2022,3,18) if old else date(2022,6,17),
        ROLL_SESSION if old and boundary else date(2022,3,11) if old else date(2022,6,10),
        exchange='MOCK_EXCHANGE', quote_unit='MOCK_INDEX_POINTS',
        verified=True, calendar_verified=True, vendor_definition_verified=True,
        boundary_verified=True, source='EXPLICIT_TEMPORARY_ENGINEERING_FIXTURE')


def bar(index, contract_id, price, source_hash, *, opening=None, volume=100_000):
    session = START + timedelta(days=index)
    opens_at = datetime.combine(session, datetime.min.time(), timezone.utc) + timedelta(hours=10)
    closes_at = opens_at + timedelta(hours=6)
    opening = price if opening is None else opening
    return SessionBar(contract_id, session, opens_at, closes_at, closes_at,
        opening, max(price,opening)+1, min(price,opening)-1, price, volume,
        source_hash, settlement=price, settlement_available_at=closes_at,
        settlement_reference_at=closes_at, session_verified=True, is_mock=True,
        next_session=session+timedelta(days=1))


@pytest.mark.parametrize('boundary', [False, True], ids=['prior-volume-cross', 'delivery-boundary'])
@pytest.mark.parametrize('direction', [1, -1], ids=['long', 'short'])
def test_decision_batch_roll_legs_offsets_costs_and_fifo(tmp_path, boundary, direction):
    fixture_path = tmp_path/'MOCK_FIXTURE_DECLARATION.json'
    fixture_path.write_text(json.dumps(dict(mock=True, research_evidence=False,
        purpose='decision-to-engine reason and accounting integration',
        direction=direction, delivery_boundary=boundary)), encoding='utf-8')
    source_hash = digest(fixture_path)
    old = contract('MESH2', old=True, boundary=boundary)
    new = contract('MESM2', old=False, boundary=boundary)
    engine = FuturesEngine({old.contract_id:old, new.contract_id:new}, EngineConfig(allow_mock=True))
    prior_pairs = []
    previous = 100
    for index in range(58):
        close = 100 if index < 55 else 100 + direction * (2 if index == 55 else 4)
        ob = bar(index, old.contract_id, close, source_hash, opening=previous)
        # Boundary case deliberately has no volume crossover.
        nb = bar(index, new.contract_id, close+20, source_hash,
                 opening=previous+20, volume=50_000 if boundary else 200_000)
        signal = replace(ob, contract_id='ESH2')
        engine.process_batch([MarketDay('SP500', signal, (ob,nb),
            mapping_verified=True, mapping_source='EXPLICIT_TEMPORARY_ENGINEERING_FIXTURE')])
        if index >= 56:
            # Producer's nonmock-shape guard is tested separately. These remain
            # artificial fixtures, declared above, and never run in real mode.
            prior_pairs.append((replace(ob,is_mock=False), replace(nb,is_mock=False)))
        previous = close
    engine._drain()
    position = engine.positions['SP500']
    before = asdict(position)
    equity_before = engine.equity()
    quantity = position.quantity
    assert quantity > 0 and position.direction == direction
    roll = decide_roll(old, new, prior_pairs[-1:] if boundary else prior_pairs,
        decision_at=prior_pairs[-1][0].available_at, next_session=ROLL_SESSION)
    assert roll.reason == ('FIVE_SESSION_DELIVERY_BOUNDARY' if boundary else 'TWO_PRIOR_SESSION_VOLUME_CROSS')
    assert roll.execution_offset == 20
    # Deliberately make the opening gap different from the causal close offset.
    old_open = previous
    new_open = previous + 20 + 5 * direction
    ob = bar(58, old.contract_id, old_open, source_hash)
    nb = bar(58, new.contract_id, new_open, source_hash)
    engine.process_batch([MarketDay('SP500', replace(ob,contract_id='ESH2'), (ob,nb), roll,
        mapping_verified=True, mapping_source='EXPLICIT_TEMPORARY_ENGINEERING_FIXTURE')])
    engine._drain(before=ob.opens_at+timedelta(microseconds=1))

    assert position.contract_id == new.contract_id
    assert position.campaign_id == before['campaign_id']
    assert position.quantity == quantity
    assert position.d0 == before['d0'] and position.q0 == before['q0']
    assert position.first_entry == pytest.approx(before['first_entry']+20)
    assert position.initial_stop == pytest.approx(before['initial_stop']+20)
    active_stop = before['pending_stop'] if before['pending_stop'] is not None else before['stop']
    assert position.stop == pytest.approx(active_stop+20)
    assert position.highest_close == pytest.approx(before['highest_close']+20)
    assert position.lowest_close == pytest.approx(before['lowest_close']+20)
    legs = [t for t in engine.result.trades if t['kind'].startswith('ROLL_')]
    assert [t['kind'] for t in legs] == ['ROLL_EXIT', 'ROLL_ENTRY']
    assert [t['contract_id'] for t in legs] == [old.contract_id,new.contract_id]
    assert all(t['quantity'] == quantity and t['reason'] == roll.reason for t in legs)
    assert legs[0]['fill_price'] == pytest.approx(old_open-direction*0.5)
    assert legs[1]['fill_price'] == pytest.approx(new_open+direction*0.5)
    assert sum(t['fee'] for t in legs) == quantity*4
    assert sum(t['slippage_cost'] for t in legs) == quantity*5
    assert engine.equity() == pytest.approx(equity_before-quantity*9)
    assert engine.result.rolls[0]['causal_offset'] == 20
    assert engine.result.rolls[0]['new_raw']-engine.result.rolls[0]['old_raw'] != 20
    assert engine.result.rolls[0]['opening_gap_is_profit'] is False
    assert not any(s['reason']=='ROLL_EVIDENCE_OR_EXECUTION_UNPROVEN' for s in engine.result.skips)

    result = engine.finish()
    result_path = tmp_path/'MOCK_ROLL_INTEGRATION_RESULT.json'
    result_path.write_text(json.dumps(asdict(result),default=str), encoding='utf-8')
    assert result.status == 'MOCK_ENGINEERING_RUN'
    assert result.reconciliation['status'] == 'PASS'
    assert result.reconciliation['provenance'] == 'MOCK'
    assert result.reconciliation['difference'] == pytest.approx(0,abs=1e-7)
    entry = next(t for t in result.trades if t['kind']=='ENTRY')
    old_realized = (legs[0]['fill_price']-entry['fill_price'])*direction*quantity*5
    new_unrealized = (new_open-legs[1]['fill_price'])*direction*quantity*5
    fees = sum(t['fee'] for t in result.trades)
    assert engine.equity() == pytest.approx(11500+old_realized+new_unrealized-fees)
    assert any(e['kind']=='VARIATION_MARGIN' for e in result.events)


def decision_fixture(tmp_path):
    path = tmp_path/'MOCK_SIGNAL_OVERLAP.json'
    path.write_text(json.dumps({'mock': True, 'research_evidence': False,
        'purpose': 'roll evidence binding regression'}), encoding='utf-8')
    sha = digest(path)
    old = contract('MESH2', old=True, boundary=False)
    new = contract('MESM2', old=False, boundary=False)
    overlaps = [(replace(bar(i,'MESH2',104,sha),is_mock=False),
                 replace(bar(i,'MESM2',124,sha,volume=200_000),is_mock=False)) for i in (56,57)]
    signal = (replace(overlaps[-1][0],contract_id='ESH2'),
              replace(overlaps[-1][1],contract_id='ESM2',close=134,high=135,low=123))
    args = dict(decision_at=overlaps[-1][0].available_at+timedelta(hours=1), next_session=ROLL_SESSION)
    return old, new, overlaps, signal, args


def test_signal_overlap_identity_source_times_values_and_payload_bind_hash(tmp_path):
    old, new, overlaps, signal, args = decision_fixture(tmp_path)
    baseline = decide_roll(old,new,overlaps,signal_overlap=signal,**args)
    assert baseline.execution_offset == 20 and baseline.signal_offset == 30
    assert baseline == decide_roll(old,new,overlaps,signal_overlap=signal,**args)
    variants = [
        (signal[0],replace(signal[1],contract_id='ESU2')),
        (replace(signal[0],source_hash='a'*64),signal[1]),
        (signal[0],replace(signal[1],source_hash='b'*64)),
        (signal[0],replace(signal[1],available_at=signal[1].available_at+timedelta(minutes=1))),
        (signal[0],replace(signal[1],close=135,high=136)),
        tuple(replace(b,session=b.session-timedelta(days=1),next_session=b.session,
            opens_at=b.opens_at-timedelta(days=1),closes_at=b.closes_at-timedelta(days=1),
            available_at=b.available_at-timedelta(days=1)) for b in signal),
    ]
    hashes = {baseline.evidence_hash}
    for changed in variants:
        decision = decide_roll(old,new,overlaps,signal_overlap=changed,**args)
        assert decision.evidence_hash != baseline.evidence_hash
        hashes.add(decision.evidence_hash)
    assert len(hashes) == len(variants)+1
    # Bind the execution payload even if a caller accidentally reuses the
    # source-object hash while altering a normalized close or decision time.
    changed_execution = [overlaps[0],(overlaps[1][0],replace(overlaps[1][1],close=125))]
    changed = decide_roll(old,new,changed_execution,signal_overlap=signal,**args)
    assert changed.execution_offset == 21 and changed.evidence_hash != baseline.evidence_hash
    changed = decide_roll(old,new,overlaps,signal_overlap=signal,
        **(args | {'decision_at':args['decision_at']+timedelta(minutes=1)}))
    assert changed.evidence_hash != baseline.evidence_hash
    without_signal = decide_roll(old,new,overlaps,**args)
    assert without_signal.evidence_hash != baseline.evidence_hash


@pytest.mark.parametrize('which', [0,1], ids=['old-signal','new-signal'])
def test_signal_overlap_future_publication_is_rejected(tmp_path, which):
    old, new, overlaps, signal, args = decision_fixture(tmp_path)
    signal = list(signal)
    signal[which] = replace(signal[which],available_at=args['decision_at']+timedelta(microseconds=1))
    with pytest.raises(ValueError,match='SIGNAL_ROLL_LOOKAHEAD'):
        decide_roll(old,new,overlaps,signal_overlap=signal,**args)


@pytest.mark.parametrize('identities', [('MESH2','ESM2'),('ESH2','GCM2'),('ESH2','ESH2'),
    ('ES.FUT','ESM2'),('ESH2','ESH2-ESM2')])
def test_signal_overlap_wrong_market_product_or_identity_is_rejected(tmp_path, identities):
    old, new, overlaps, signal, args = decision_fixture(tmp_path)
    signal = tuple(replace(b,contract_id=identity) for b,identity in zip(signal,identities))
    with pytest.raises(ValueError,match='SIGNAL_ROLL_CONTRACT_IDENTITY_MISMATCH'):
        decide_roll(old,new,overlaps,signal_overlap=signal,**args)
