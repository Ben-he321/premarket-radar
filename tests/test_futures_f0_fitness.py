"""Explicit MOCK unit cases; never written to the research data directory."""
import unittest
from datetime import date, datetime, timezone

from src.futures_f0.engine import FuturesEngine
from src.futures_f0.fitness import COSTS, adverse_fill, liquidity, one_contract
from src.futures_f0.model import ContractSpec, EngineConfig
from src.futures_f0.run_fitness_audit import diagnostic_execution,verify_price_records


class FitnessTest(unittest.TestCase):
    def case(self, **kw):
        args=dict(raw_price=2903.25,multiplier=1,tick_size=.25,
                  margin_fraction=.1,direction=1,cost='BASE_2T')
        args.update(kw);return one_contract(**args)

    def test_missing_risk_cannot_be_allowed(self):
        r=self.case()
        self.assertEqual(r['financial_status'],'INSUFFICIENT_EVIDENCE')
        self.assertEqual(r['risk']['status'],'UNKNOWN')
        self.assertFalse(r['actual_rejection_event'])

    def test_mes_layer_not_initial_capital_is_binding(self):
        r=self.case(raw_price=5206.5,multiplier=5)
        self.assertEqual(r['raw_margin_usd'],'2603.25')
        self.assertEqual(r['layer_margin_integer_cap'],0)
        self.assertEqual(r['financial_status'],'RULE_NOT_ALLOWED')
        self.assertFalse(r['actual_order_created'])

    def test_margin_boundary_uses_slipped_integer_contract(self):
        r=self.case(raw_price=1000,multiplier=10,tick_size=.1)
        self.assertEqual(r['layer_margin_integer_cap'],0)
        self.assertEqual(r['raw_margin_usd'],'1000.0')
        self.assertGreater(float(r['entry_margin_usd']),1000)

    def test_corn_cent_unit_tick_and_fee(self):
        r=self.case(raw_price=454,multiplier=5,tick_size=.5)
        self.assertEqual(float(r['tick_value_usd']),2.5)
        self.assertEqual(float(r['roundtrip_cost_on_tick_usd']),14)
        self.assertEqual(float(r['zero_gap_atr_ceiling_illustration']),10.1)

    def test_real_liquidity_requirements_unknown_and_zero_distinct(self):
        self.assertEqual(liquidity([10000]*19)['status'],'UNKNOWN')
        self.assertEqual(liquidity([10000]*19+[None])['status'],'UNKNOWN')
        self.assertEqual(liquidity([10000]*19+[0])['status'],'RULE_NOT_ALLOWED')
        self.assertEqual(liquidity([999]*20)['integer_cap'],0)
        self.assertEqual(liquidity([1000]*20)['integer_cap'],1)

    def test_actual_gap_and_stress_not_silently_zero(self):
        a=self.case(atr=10,previous_execution_close=2900)
        b=self.case(atr=10,previous_execution_close=2900,cost='STRESS_4T')
        c=self.case(atr=10,previous_execution_close=2900,cost='DOUBLE_COMMISSION_4T')
        self.assertEqual(float(a['risk']['initial_size_loss_usd']),28.25)
        self.assertEqual(b['risk']['initial_size_loss_usd'],c['risk']['initial_size_loss_usd'])
        self.assertEqual(float(c['risk']['scenario_stop_proxy_loss_usd'])-float(b['risk']['scenario_stop_proxy_loss_usd']),4)

    def test_crossed_stop_is_not_resized(self):
        r=self.case(raw_price=2870,atr=10,previous_execution_close=2900)
        self.assertTrue(r['risk']['open_crosses_stop'])
        self.assertEqual(r['risk']['initial_risk_integer_cap'],0)

    def test_engine_fill_risk_and_one_contract_parity_mock(self):
        for direction in (1,-1):
            for alpha in (.1,.2):
                for cost,(slip,commission) in COSTS.items():
                    with self.subTest(direction=direction,alpha=alpha,cost=cost):
                        # Explicit MOCK spec: never qualifies historical input.
                        spec=ContractSpec('MOCK1','GOLD','MOCK',1,.25,None,date(2025,12,1),None)
                        config=EngineConfig(slippage_ticks=slip,commission_multiplier=commission,
                            initial_margin_fraction=alpha,margin_scenario=f'ASSUMED_MARGIN_{int(alpha*100)}_PERCENT')
                        engine=FuturesEngine({'MOCK1':spec},config)
                        engine.volumes['MOCK1'].extend([1000]*20)
                        raw=2903.25;previous=2900;atr=10.03;stop=previous-direction*2*atr
                        r=self.case(raw_price=raw,atr=atr,previous_execution_close=previous,
                            direction=direction,margin_fraction=alpha,cost=cost,prior_volumes=[1000]*20)
                        self.assertAlmostEqual(float(r['hypothetical_entry_fill']),engine._fill(raw,direction,spec))
                        self.assertAlmostEqual(float(r['entry_margin_usd']),engine._margin(spec,engine._fill(raw,direction,spec)))
                        actual=direction*(engine._fill(raw,direction,spec)-engine._fill(stop,-direction,spec))+2*engine._commission(spec)
                        self.assertAlmostEqual(float(r['risk']['scenario_stop_proxy_loss_usd']),actual)
                        quantity,_=engine._size(spec,raw,stop,direction,datetime(2025,3,4,tzinfo=timezone.utc),max_qty=1)
                        self.assertEqual(quantity,1)
                        self.assertEqual(engine.result.trades,[])

    def test_reject_nonfinite_and_fractional_direction(self):
        for kw in ({'raw_price':'NaN'},{'multiplier':0},{'direction':.5},{'margin_fraction':.05}):
            with self.assertRaises(ValueError):self.case(**kw)

    def test_new_micro_corn_cannot_have_twenty_prior_sessions(self):
        r=self.case(root_launch='2025-02-24',decision_session='2025-03-04')
        self.assertEqual(r['financial_status'],'RULE_NOT_ALLOWED')
        self.assertEqual(r['liquidity']['reason'],'INSUFFICIENT_SESSIONS_SINCE_ROOT_LAUNCH')
        self.assertIsNone(r['liquidity']['median_volume'])
        self.assertEqual(r['liquidity']['maximum_possible_prior_dates'],8)

    def test_older_root_does_not_prove_liquidity(self):
        r=self.case(root_launch='2025-01-13',decision_session='2025-03-04')
        self.assertEqual(r['liquidity']['status'],'UNKNOWN')

    def test_partial_calendar_cannot_authorize_open(self):
        r=diagnostic_execution(None,'MOCK',(1,1),{'coverage_qualified':False},{},.25)
        self.assertFalse(r['tradable_open']);self.assertFalse(r['tradable_stop'])
        self.assertTrue(r['not_evidence_of_exchange_halt'])

    def test_missing_statistics_cannot_authorize_open(self):
        class MockCatalog:
            def interval_coverage(self,*args):return {'status':'NOT_REQUESTED'}
        cohort=dict(coverage_qualified=True,reset_event_at='2025-03-03T22:00:00Z',
                    segments=[['2025-03-03T23:00:00Z','2025-03-04T22:00:00Z']])
        r=diagnostic_execution(MockCatalog(),'MOCK',(1,1),cohort,{'price_coverage':'COMPLETE_OBSERVED_BUCKETS'},.25)
        self.assertFalse(r['tradable_open'])

    def test_derived_prices_require_exact_raw_reference_and_aggregate(self):
        # Explicit miniature MOCK records, no source or receipt qualification.
        class MockCatalog:
            def rows(self,*args,**kwargs):
                return iter([dict(schema='ohlcv-1h',source_sha256='mock-file',source_line=2,
                    raw_record_sha256='mock-record',ts_event='2025-03-04T01:00:00.000000000Z',
                    ts_event_ns=1741050000000000000,open='454',high='455',low='453',close='454.5',volume=100)])
        prices=dict(records=[dict(schema='ohlcv-1h',source_sha256='mock-file',source_line=2,
                    raw_record_sha256='mock-record',bucket_start='2025-03-04T01:00:00.000000000Z')],
                    session_ohlcv=dict(open='454',high='455',low='453',close='454.5',volume=100))
        self.assertEqual(verify_price_records(prices,MockCatalog(),(1,1))['bucket_count'],1)
        prices['records'][0]['raw_record_sha256']='tampered'
        with self.assertRaisesRegex(ValueError,'HASH_OR_TIME'):verify_price_records(prices,MockCatalog(),(1,1))
        prices['records'][0]['raw_record_sha256']='mock-record';prices['session_ohlcv']['volume']=101
        with self.assertRaisesRegex(ValueError,'AGGREGATE'):verify_price_records(prices,MockCatalog(),(1,1))


if __name__=='__main__':unittest.main()
