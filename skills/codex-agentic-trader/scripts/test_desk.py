#!/usr/bin/env python3
"""Decision/risk/audit regression tests.  No broker or network access."""
import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import behavior_review
import cadence_gate
import decision_gate
import journal_append
import performance_review

POLICY = decision_gate.load_policy()
NOW = datetime(2026, 8, 13, 15, 42, tzinfo=ZoneInfo("America/New_York"))


def base_payload():
    return {
        "schema_version": "1.0",
        "mode": "decide",
        "as_of_et": "2026-08-13T15:40:00-04:00",
        "data_age_seconds": 120,
        "account": {
            "equity": 5000.0,
            "cash_available_settled": 1500.0,
            "broker_buying_power": 1500.0,
            "unleveraged_buying_power": 1500.0,
            "account_type": "cash",
            "margin_enabled": False,
            "strategy_external_capital": 5000.0,
            "peak_equity_adjusted": 5200.0,
            "positions": [
                {"symbol": "VTI", "value": 2000.0, "sellable_value": 2000.0,
                 "asset_class": "etf"},
                {"symbol": "SGOV", "value": 800.0, "sellable_value": 800.0,
                 "asset_class": "cash_equiv"},
            ],
            "open_orders": [],
        },
        "market_state": {"benchmark_a_pass": True, "benchmark_b_pass": True},
        "history": {"orders_today": 1, "turnover_today": 300.0,
                    "used_decision_keys": []},
        "proposal": {
            "symbol": "ABCD", "side": "buy", "amount": 500.0,
            "asset_class": "stock", "trigger": "qualified_candidate",
            "order_type": "market_day", "quantity": None, "limit_price": None,
            "thesis": "strong evidence", "counter_thesis": "macro reversal",
            "invalidation": "daily close below 200dma",
            "declares": {
                "price": 25.0, "market_cap": 2e10, "avg_daily_volume": 2e6,
                "above_50dma": True, "above_200dma": True,
                "ret_3m_positive": True, "ret_6m_positive": True,
                "rel_strength_vs_benchmark_20d": True,
                "days_to_earnings": 10, "no_thesis_breaking_news": True,
                "is_us_listed_common_stock": True, "tradable_for_account": True,
                "is_leveraged_or_inverse": False, "is_otc": False,
                "wash_sale_conflict": False,
            },
        },
    }


def run(payload):
    return decision_gate.evaluate(payload, POLICY, now=NOW)


class DecisionAllowTests(unittest.TestCase):
    def test_stock_buy(self):
        result = run(base_payload())
        self.assertTrue(result["would_allow"], result["violations"])
        self.assertEqual(result["execution_capability"], "none")

    def test_etf_rebalance(self):
        payload = base_payload()
        payload["proposal"].update(symbol="QQQM", asset_class="etf",
                                   trigger="weight_deviation")
        payload["proposal"]["declares"] = {
            "is_leveraged_or_inverse": False, "is_otc": False,
            "wash_sale_conflict": False, "weight_deviation_pct": 0.04,
        }
        self.assertTrue(run(payload)["would_allow"])

    def test_stock_risk_exit(self):
        payload = base_payload()
        payload["account"]["positions"].append(
            {"symbol": "ABCD", "value": 600.0, "sellable_value": 600.0,
             "asset_class": "stock"})
        payload["proposal"].update(side="sell", trigger="thesis_break")
        payload["proposal"]["declares"] = {
            "is_leveraged_or_inverse": False, "is_otc": False,
            "exit_signal": "thesis_invalidated",
        }
        self.assertTrue(run(payload)["would_allow"])


class DecisionRejectTests(unittest.TestCase):
    def expect(self, mutate, code):
        payload = base_payload(); mutate(payload)
        result = run(payload)
        self.assertFalse(result["would_allow"])
        self.assertIn(code, result["violations"])

    def test_bad_mode(self):
        self.expect(lambda p: p.update(mode="research"), "mode_must_be_decide")

    def test_stale_timestamp(self):
        self.expect(lambda p: p.update(as_of_et="2026-08-13T14:00:00-04:00"),
                    "snapshot_time_out_of_range")

    def test_declared_age_lie(self):
        self.expect(lambda p: p.update(data_age_seconds=1), "declared_data_age_mismatch")

    def test_naive_timestamp(self):
        self.expect(lambda p: p.update(as_of_et="2026-08-13T15:40:00"),
                    "invalid_as_of_et")

    def test_capital_cap(self):
        self.expect(lambda p: p["account"].update(strategy_external_capital=5000.01),
                    "strategy_capital_over_cap_or_invalid")

    def test_margin_account(self):
        self.expect(lambda p: p["account"].update(account_type="margin"),
                    "cash_account_not_verified")

    def test_leverage_buying_power(self):
        self.expect(lambda p: p["account"].update(broker_buying_power=3000),
                    "leverage_buying_power_detected")

    def test_cash_not_authoritative_bp(self):
        self.expect(lambda p: p["account"].update(cash_available_settled=1400),
                    "settled_cash_not_unleveraged_buying_power")

    def test_nan(self):
        self.expect(lambda p: p["account"].update(equity=float("nan")), "invalid_equity")

    def test_position_missing_sellable(self):
        self.expect(lambda p: p["account"]["positions"][0].pop("sellable_value"),
                    "invalid_position_entry")

    def test_duplicate_open_order(self):
        def mutate(p):
            row = {"order_id": "o1", "symbol": "VTI", "side": "buy",
                   "asset_class": "etf", "remaining_notional": 50.0}
            p["account"]["open_orders"] = [row, copy.deepcopy(row)]
        self.expect(mutate, "invalid_open_order_entry")

    def test_open_same_symbol(self):
        def mutate(p):
            p["account"]["open_orders"] = [{"order_id": "o1", "symbol": "ABCD",
                "side": "buy", "asset_class": "stock", "remaining_notional": 100.0}]
        self.expect(mutate, "open_order_for_symbol_exists")

    def test_pending_buys_reserve_cash(self):
        def mutate(p):
            p["account"]["open_orders"] = [{"order_id": "o1", "symbol": "QQQM",
                "side": "buy", "asset_class": "etf", "remaining_notional": 1000.0}]
        self.expect(mutate, "insufficient_settled_cash_or_buffer")

    def test_pending_sell_reserves_position(self):
        def mutate(p):
            p["account"]["positions"].append({"symbol": "ABCD", "value": 600.0,
                "sellable_value": 600.0, "asset_class": "stock"})
            p["account"]["open_orders"] = [{"order_id": "o1", "symbol": "ABCD",
                "side": "sell", "asset_class": "stock", "remaining_notional": 300.0}]
            p["proposal"].update(side="sell", trigger="thesis_break", amount=500.0)
            p["proposal"]["declares"] = {"is_leveraged_or_inverse": False,
                "is_otc": False, "exit_signal": "thesis_invalidated"}
        self.expect(mutate, "sell_exceeds_available_position")

    def test_penny_stock(self):
        self.expect(lambda p: p["proposal"]["declares"].update(price=4.99),
                    "price_below_min")

    def test_not_common_stock(self):
        self.expect(lambda p: p["proposal"]["declares"].update(
            is_us_listed_common_stock=False),
            "entry_flag_failed_is_us_listed_common_stock")

    def test_wash_sale(self):
        self.expect(lambda p: p["proposal"]["declares"].update(wash_sale_conflict=True),
                    "wash_sale_conflict_or_unknown")

    def test_earnings_window(self):
        self.expect(lambda p: p["proposal"]["declares"].update(days_to_earnings=2),
                    "too_close_to_earnings")

    def test_weight_trigger_needs_3pp(self):
        def mutate(p):
            p["proposal"].update(symbol="QQQM", asset_class="etf", trigger="weight_deviation")
            p["proposal"]["declares"] = {"is_leveraged_or_inverse": False,
                "is_otc": False, "wash_sale_conflict": False,
                "weight_deviation_pct": 0.02}
        self.expect(mutate, "weight_deviation_below_trigger")

    def test_bad_exit_signal(self):
        def mutate(p):
            p["account"]["positions"].append({"symbol": "ABCD", "value": 600.0,
                "sellable_value": 600.0, "asset_class": "stock"})
            p["proposal"].update(side="sell", trigger="thesis_break")
            p["proposal"]["declares"] = {"is_leveraged_or_inverse": False,
                "is_otc": False, "exit_signal": "because_i_feel_like_it"}
        self.expect(mutate, "stock_exit_signal_missing_or_invalid")

    def test_limit_needs_quantity(self):
        self.expect(lambda p: p["proposal"].update(order_type="limit_day", limit_price=25),
                    "limit_quantity_required")

    def test_limit_notional(self):
        self.expect(lambda p: p["proposal"].update(order_type="limit_day",
                    quantity=10, limit_price=25), "limit_notional_mismatch")

    def test_market_rejects_quantity(self):
        self.expect(lambda p: p["proposal"].update(quantity=20),
                    "market_order_quantity_or_limit_forbidden")

    def test_single_stock_cap(self):
        self.expect(lambda p: p["proposal"].update(amount=800), "single_stock_over_cap")

    def test_cash_buffer(self):
        def mutate(p):
            p["account"].update(cash_available_settled=500,
                                broker_buying_power=500, unleveraged_buying_power=500)
        self.expect(mutate, "insufficient_settled_cash_or_buffer")

    def test_daily_order_integer(self):
        self.expect(lambda p: p["history"].update(orders_today=1.5), "invalid_orders_today")

    def test_daily_turnover(self):
        self.expect(lambda p: p["history"].update(turnover_today=1200),
                    "daily_turnover_over_cap")

    def test_duplicate_decision(self):
        def mutate(p):
            p["history"]["used_decision_keys"] = [decision_gate.compute_decision_key(
                "2026-08-13", "ABCD", "buy", "qualified_candidate", 500)]
        self.expect(mutate, "duplicate_decision")

    def test_full_account_like_string(self):
        self.expect(lambda p: p["proposal"].update(thesis="acct 1234-5678-9012"),
                    "account_like_identifier_present")


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = str(Path(self.tmp.name) / "journal.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def test_chain(self):
        first = journal_append.append(self.log, {"type": "decision", "symbol": "VTI"})
        second = journal_append.append(self.log, {"type": "fill", "symbol": "VTI"})
        self.assertEqual(second["prev_hash"], first["hash"])

    def test_tamper(self):
        journal_append.append(self.log, {"type": "decision", "symbol": "VTI"})
        lines = Path(self.log).read_text().splitlines()
        entry = json.loads(lines[0]); entry["event"]["symbol"] = "QQQM"
        Path(self.log).write_text(json.dumps(entry) + "\n")
        with self.assertRaises(ValueError):
            journal_append.append(self.log, {"type": "decision", "symbol": "VTI"})

    def test_sensitive_account_key(self):
        with self.assertRaises(ValueError):
            journal_append.append(self.log, {"type": "fill", "account-url": "x"})

    def test_non_finite_numeric(self):
        with self.assertRaises(ValueError):
            journal_append.append(self.log, {"type": "fill", "memo": float("inf")})

    def test_masked_ref(self):
        row = journal_append.append(self.log, {"type": "decision",
            "account_ref_masked": "****0000"})
        self.assertEqual(row["seq"], 0)

    def test_unknown_event_type(self):
        with self.assertRaises(ValueError):
            journal_append.append(self.log, {"type": "anything_goes"})


class BehaviorTests(unittest.TestCase):
    def test_insufficient_data(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        log = str(Path(tmp.name) / "j.jsonl")
        journal_append.append(log, {"type": "fill", "date_et": "2026-08-01",
                                    "symbol": "A", "side": "buy", "amount": 100.0})
        findings = behavior_review.analyze(behavior_review.load_fills(log), 6)
        self.assertTrue(all(not row["flag"] for row in findings))


class CadenceTests(unittest.TestCase):
    def row(self, clock, buying_power=100.0, trading=True, urgent=False):
        return cadence_gate.evaluate({
            "as_of_et": f"2026-08-13T{clock}:00-04:00",
            "is_trading_day": trading,
            "unleveraged_buying_power": buying_power,
            "urgent_risk_event": urgent,
        }, POLICY)

    def test_two_full_analyses(self):
        self.assertEqual(self.row("08:30")["action"], "full_preopen_analysis")
        close = self.row("17:30")
        self.assertTrue(close["full_analysis"])
        self.assertTrue(close["send_daily_report"])
        self.assertTrue(close["run_evolution"])

    def test_sufficient_buying_power_every_half_hour(self):
        result = self.row("10:30", buying_power=20.0)
        self.assertTrue(result["operation_decision"])
        self.assertTrue(result["trade_permitted_by_cadence"])

    def test_insufficient_buying_power_between_slots(self):
        result = self.row("10:30", buying_power=19.99)
        self.assertEqual(result["action"], "account_order_probe")
        self.assertFalse(result["operation_decision"])

    def test_insufficient_buying_power_two_hour_slot(self):
        result = self.row("12:00", buying_power=19.99)
        self.assertTrue(result["operation_decision"])
        self.assertTrue(result["trade_permitted_by_cadence"])

    def test_urgent_event_does_not_wait(self):
        result = self.row("11:30", buying_power=0.0, urgent=True)
        self.assertTrue(result["operation_decision"])

    def test_closed_day_never_operates(self):
        result = self.row("12:00", buying_power=100.0, trading=False)
        self.assertFalse(result["operation_decision"])
        self.assertFalse(result["trade_permitted_by_cadence"])


class PerformanceReviewTests(unittest.TestCase):
    def period(self, days=20, strategy=0.05, voo=0.03, qqq=0.04):
        return {"label": f"{days}d", "trading_days": days,
                "strategy_return": strategy, "voo_return": voo,
                "qqq_return": qqq, "same_interval": True,
                "cash_flow_adjusted": True, "net_of_costs": True}

    def test_can_claim_only_after_twenty_days_and_beating_both(self):
        result = performance_review.evaluate({"periods": [self.period()]}, POLICY)
        self.assertTrue(result["periods"][0]["can_claim_outperformance"])
        self.assertFalse(result["expand_qualified_stock_search"])

    def test_underperformance_expands_search_not_trade_authority(self):
        result = performance_review.evaluate({"periods": [
            self.period(days=5, strategy=0.01, voo=0.02, qqq=0.03)]}, POLICY)
        self.assertTrue(result["expand_qualified_stock_search"])
        self.assertFalse(result["trade_authorized"])

    def test_short_history_cannot_claim(self):
        result = performance_review.evaluate({"periods": [self.period(days=5)]}, POLICY)
        self.assertFalse(result["periods"][0]["can_claim_outperformance"])

    def test_requires_cash_flow_and_cost_alignment(self):
        row = self.period(); row["cash_flow_adjusted"] = False
        with self.assertRaises(ValueError):
            performance_review.evaluate({"periods": [row]}, POLICY)


if __name__ == "__main__":
    unittest.main(verbosity=1)
