"""Prior-information-only roll decision; actual trade legs belong to the engine."""
from .model import RollInstruction
from .runtime import canonical_hash


def decide_roll(old_spec, new_spec, overlaps, *, decision_at, next_session,
                signal_overlap=None):
    """Consume at most two known paired full sessions, never current open prices.

    ``overlaps`` contains (old SessionBar, new SessionBar). No synthesized old
    prices or multiplier conversion is accepted. signal_overlap is optional
    (old standard bar,new standard bar) for its independent causal link.
    """
    if old_spec.market != new_spec.market or old_spec.root != new_spec.root:
        raise ValueError('CROSS_PRODUCT_ROLL_REQUIRES_SEPARATE_VERIFIED_MIGRATION')
    if old_spec.quote_unit != new_spec.quote_unit or old_spec.multiplier != new_spec.multiplier:
        raise ValueError('ROLL_UNITS_MISMATCH')
    if not (old_spec.boundary_verified and new_spec.boundary_verified and
            old_spec.vendor_definition_verified and new_spec.vendor_definition_verified):
        raise ValueError('ROLL_BOUNDARY_OR_DEFINITION_UNKNOWN')
    if old_spec.safe_exit_session is None or new_spec.safe_exit_session is None:
        raise ValueError('ROLL_SAFE_DATE_UNKNOWN')
    if new_spec.last_trade <= old_spec.last_trade or next_session >= new_spec.safe_exit_session:
        raise ValueError('ROLL_TARGET_NOT_VALID_NEXT_EXPIRY')
    if not new_spec.listed <= next_session:
        raise ValueError('ROLL_TARGET_NOT_YET_LISTED')
    pairs = tuple(overlaps)
    if not 1 <= len(pairs) <= 2:
        raise ValueError('ROLL_REQUIRES_ONE_OR_TWO_PRIOR_OVERLAPS')
    previous_session = None
    for old, new in pairs:
        if old.contract_id != old_spec.contract_id or new.contract_id != new_spec.contract_id:
            raise ValueError('ROLL_CONTRACT_MAPPING_MISMATCH')
        if old.session != new.session or old.session >= next_session:
            raise ValueError('ROLL_OVERLAP_NOT_PREVIOUS_SESSION')
        if previous_session is not None and (old.session <= previous_session or prior_old.next_session != old.session):
            raise ValueError('ROLL_VOLUME_DAYS_NOT_CONSECUTIVE')
        previous_session, prior_old = old.session, old
        if any(b.status != 'QUALIFIED' or not b.session_verified or b.available_at > decision_at for b in (old, new)):
            raise ValueError('ROLL_OVERLAP_UNKNOWN_OR_FUTURE')
        if any(b.is_mock for b in (old, new)):
            raise ValueError('MOCK_ROLL_NOT_REAL_INPUT')
    last_old, last_new = pairs[-1]
    if last_old.next_session != next_session:
        raise ValueError('ROLL_NEXT_SESSION_NOT_VERIFIED')
    boundary = next_session >= old_spec.safe_exit_session
    volume = (len(pairs) == 2 and all(isinstance(o.volume, int) and isinstance(n.volume, int)
              and n.volume > o.volume >= 0 for o, n in pairs))
    if not boundary and not volume:
        return None
    signal_id, signal_offset = None, 0.0
    if signal_overlap:
        so, sn = signal_overlap
        if so.session != sn.session or so.session >= next_session or max(so.available_at, sn.available_at) > decision_at:
            raise ValueError('SIGNAL_ROLL_LOOKAHEAD')
        if any(b.status != 'QUALIFIED' or b.is_mock or not b.session_verified for b in (so, sn)):
            raise ValueError('SIGNAL_ROLL_UNVERIFIED')
        signal_id, signal_offset = sn.contract_id, sn.close - so.close
    evidence = dict(old=last_old.source_hash, new=last_new.source_hash,
                    previous=[(o.source_hash,n.source_hash) for o,n in pairs],
                    decision_at=decision_at.isoformat(), next_session=next_session.isoformat(),
                    safe_exit=old_spec.safe_exit_session.isoformat())
    return RollInstruction(old_spec.market, old_spec.contract_id, new_spec.contract_id,
                           decision_at, last_new.close-last_old.close,
                           'DELIVERY_FIVE_SESSION_BOUNDARY' if boundary else 'TWO_COMPLETED_DAY_VOLUME_CROSS',
                           canonical_hash(evidence), signal_id, signal_offset)
