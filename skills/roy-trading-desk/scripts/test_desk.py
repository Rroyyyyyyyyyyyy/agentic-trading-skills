#!/usr/bin/env python3
"""roy-trading-desk 回归测试：决策闸门 / 链式日志 / 行为画像。"""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import behavior_review
import decision_gate
import journal_append

POLICY = decision_gate.load_policy()


def base_payload():
    return {
        "schema_version": "1.0",
        "mode": "decide",
        "as_of_et": "2026-08-13T15:40:00-04:00",
        "data_age_seconds": 120,
        "account": {
            "equity": 5000.0,
            "cash_available_settled": 1500.0,
            "peak_equity_adjusted": 5200.0,
            "positions": [
                {"symbol": "VTI", "value": 2000.0, "asset_class": "etf"},
                {"symbol": "SGOV", "value": 800.0, "asset_class": "cash_equiv"},
            ],
        },
        "market_state": {"benchmark_a_pass": True, "benchmark_b_pass": True},
        "history": {"orders_today": 1, "turnover_today": 300.0, "used_decision_keys": []},
        "proposal": {
            "symbol": "ABCD",
            "side": "buy",
            "amount": 500.0,
            "asset_class": "stock",
            "trigger": "qualified_candidate",
            "order_type": "market_day",
            "thesis": "论点",
            "counter_thesis": "反论点",
            "invalidation": "收盘跌破 23.5",
            "declares": {
                "price": 25.0,
                "market_cap": 2e10,
                "avg_daily_volume": 2e6,
                "above_50dma": True,
                "above_200dma": True,
                "ret_3m_positive": True,
                "ret_6m_positive": True,
                "rel_strength_vs_benchmark_20d": True,
                "days_to_earnings": 10,
                "no_thesis_breaking_news": True,
                "is_leveraged_or_inverse": False,
                "is_otc": False,
            },
        },
    }


class GateAllowTests(unittest.TestCase):
    def test_valid_stock_buy_allowed(self):
        r = decision_gate.evaluate(base_payload(), POLICY)
        self.assertTrue(r["would_allow"], r["violations"])
        self.assertEqual(r["execution_capability"], "none")
        self.assertIn("manual_card", r)
        self.assertIn("手动执行", r["manual_card"]["note"])

    def test_etf_buy_allowed_minimal_declares(self):
        p = base_payload()
        p["proposal"].update({"symbol": "QQQM", "asset_class": "etf",
                              "trigger": "weight_deviation"})
        p["proposal"]["declares"] = {"is_leveraged_or_inverse": False, "is_otc": False}
        r = decision_gate.evaluate(p, POLICY)
        self.assertTrue(r["would_allow"], r["violations"])

    def test_sell_allowed_without_entry_declares(self):
        p = base_payload()
        p["account"]["positions"].append({"symbol": "ABCD", "value": 600.0, "asset_class": "stock"})
        p["proposal"].update({"side": "sell", "trigger": "thesis_break"})
        p["proposal"]["declares"] = {"is_leveraged_or_inverse": False, "is_otc": False}
        r = decision_gate.evaluate(p, POLICY)
        self.assertTrue(r["would_allow"], r["violations"])


class GateRejectTests(unittest.TestCase):
    def _expect_violation(self, payload, code_substr):
        r = decision_gate.evaluate(payload, POLICY)
        self.assertFalse(r["would_allow"])
        self.assertTrue(any(code_substr in v for v in r["violations"]),
                        f"{code_substr} not in {r['violations']}")

    def test_mode_research_rejected(self):
        p = base_payload(); p["mode"] = "research"
        self._expect_violation(p, "mode_must_be_decide")

    def test_stale_data(self):
        p = base_payload(); p["data_age_seconds"] = 999
        self._expect_violation(p, "stale_or_invalid_data_age")

    def test_nan_equity(self):
        p = base_payload(); p["account"]["equity"] = float("nan")
        self._expect_violation(p, "invalid_equity")

    def test_account_like_identifier(self):
        p = base_payload(); p["proposal"]["thesis"] = "账户 123456789 的论点"
        self._expect_violation(p, "account_like_identifier_present")

    def test_leveraged_etf_rejected(self):
        p = base_payload()
        p["proposal"]["declares"]["is_leveraged_or_inverse"] = True
        self._expect_violation(p, "leveraged_or_inverse_forbidden")

    def test_missing_leveraged_declaration_rejected(self):
        p = base_payload()
        del p["proposal"]["declares"]["is_leveraged_or_inverse"]
        self._expect_violation(p, "leveraged_or_inverse_forbidden")

    def test_penny_stock(self):
        p = base_payload(); p["proposal"]["declares"]["price"] = 3.0
        self._expect_violation(p, "price_below_min")

    def test_small_cap(self):
        p = base_payload(); p["proposal"]["declares"]["market_cap"] = 5e9
        self._expect_violation(p, "market_cap_below_min")

    def test_below_200dma(self):
        p = base_payload(); p["proposal"]["declares"]["above_200dma"] = False
        self._expect_violation(p, "entry_flag_failed_above_200dma")

    def test_earnings_too_close(self):
        p = base_payload(); p["proposal"]["declares"]["days_to_earnings"] = 1
        self._expect_violation(p, "too_close_to_earnings")

    def test_invalid_trigger(self):
        p = base_payload(); p["proposal"]["trigger"] = "fomo"
        self._expect_violation(p, "invalid_trigger")

    def test_missing_thesis(self):
        p = base_payload(); p["proposal"]["thesis"] = "  "
        self._expect_violation(p, "missing_thesis")

    def test_single_stock_over_cap(self):
        p = base_payload(); p["proposal"]["amount"] = 900.0  # 900/5000=18% > 15%
        self._expect_violation(p, "single_stock_over_cap")

    def test_stocks_total_over_cap(self):
        p = base_payload()
        p["account"]["positions"].append({"symbol": "WXYZ", "value": 1100.0, "asset_class": "stock"})
        p["proposal"]["amount"] = 500.0  # (1100+500)/5000=32% > 30%
        self._expect_violation(p, "stocks_total_over_cap")

    def test_too_many_stock_positions(self):
        p = base_payload()
        p["account"]["positions"] += [
            {"symbol": "AAAA", "value": 300.0, "asset_class": "stock"},
            {"symbol": "BBBB", "value": 300.0, "asset_class": "stock"},
        ]
        self._expect_violation(p, "too_many_stock_positions")

    def test_equity_exposure_over_market_state_cap(self):
        p = base_payload()
        p["market_state"] = {"benchmark_a_pass": False, "benchmark_b_pass": False}  # cap 20%
        self._expect_violation(p, "equity_exposure_over_cap")

    def test_drawdown_tier_lowers_cap(self):
        p = base_payload()
        p["account"]["equity"] = 3800.0            # dd vs 5200 ≈ 26.9% → cap 40%
        p["account"]["peak_equity_adjusted"] = 5200.0
        p["proposal"]["amount"] = 300.0
        # 现有 etf 2000/3800=52.6% 已超 40% cap
        r = decision_gate.evaluate(p, POLICY)
        self.assertFalse(r["would_allow"])
        self.assertEqual(r["derived"]["stock_cap_drawdown"], 0.4)
        self.assertIn("equity_exposure_over_cap", r["violations"])

    def test_deepest_drawdown_zero_cap(self):
        p = base_payload()
        p["account"]["equity"] = 3300.0            # dd ≈ 36.5% → cap 0
        r = decision_gate.evaluate(p, POLICY)
        self.assertEqual(r["derived"]["stock_cap_drawdown"], 0.0)
        self.assertFalse(r["would_allow"])

    def test_insufficient_cash(self):
        p = base_payload(); p["account"]["cash_available_settled"] = 100.0
        self._expect_violation(p, "insufficient_settled_cash")

    def test_daily_order_limit(self):
        p = base_payload(); p["history"]["orders_today"] = 6
        self._expect_violation(p, "daily_order_limit_reached")

    def test_daily_turnover_cap(self):
        p = base_payload(); p["history"]["turnover_today"] = 1200.0  # +500 > 1500=30%*5000
        self._expect_violation(p, "daily_turnover_over_cap")

    def test_risk_exit_sell_bypasses_frequency(self):
        p = base_payload()
        p["account"]["positions"].append({"symbol": "ABCD", "value": 600.0, "asset_class": "stock"})
        p["proposal"].update({"side": "sell", "trigger": "thesis_break"})
        p["proposal"]["declares"] = {"is_leveraged_or_inverse": False, "is_otc": False}
        p["history"].update({"orders_today": 6, "turnover_today": 2000.0})
        r = decision_gate.evaluate(p, POLICY)
        self.assertTrue(r["would_allow"], r["violations"])

    def test_naked_short_rejected(self):
        p = base_payload()
        p["proposal"].update({"side": "sell", "trigger": "thesis_break"})
        p["proposal"]["declares"] = {"is_leveraged_or_inverse": False, "is_otc": False}
        self._expect_violation(p, "sell_position_not_held")

    def test_sell_exceeds_position_rejected(self):
        p = base_payload()
        p["account"]["positions"].append({"symbol": "ABCD", "value": 300.0, "asset_class": "stock"})
        p["proposal"].update({"side": "sell", "trigger": "thesis_break", "amount": 500.0})
        p["proposal"]["declares"] = {"is_leveraged_or_inverse": False, "is_otc": False}
        self._expect_violation(p, "sell_exceeds_position")

    def test_duplicate_decision_key(self):
        p = base_payload()
        key = decision_gate.compute_decision_key("2026-08-13", "ABCD", "buy",
                                                 "qualified_candidate", 500.0)
        p["history"]["used_decision_keys"] = [key]
        self._expect_violation(p, "duplicate_decision")

    def test_ghost_position_cannot_bypass_max_positions(self):
        # H1 回归：同名 cash_equiv 幽灵仓位不得让第 3 只个股绕过持仓数上限
        p = base_payload()
        p["account"]["positions"] += [
            {"symbol": "AAAA", "value": 300.0, "asset_class": "stock"},
            {"symbol": "BBBB", "value": 300.0, "asset_class": "stock"},
            {"symbol": "ABCD", "value": 1.0, "asset_class": "cash_equiv"},
        ]
        self._expect_violation(p, "too_many_stock_positions")

    def test_grouped_digits_account_scan(self):
        p = base_payload(); p["proposal"]["thesis"] = "账户 1234 5678 9012 的论点"
        self._expect_violation(p, "account_like_identifier_present")

    def test_long_digits_account_scan(self):
        p = base_payload(); p["proposal"]["thesis"] = "id 123456789012345678901"
        self._expect_violation(p, "account_like_identifier_present")

    def test_hex_decision_key_not_flagged(self):
        p = base_payload()
        p["history"]["used_decision_keys"] = ["0" * 63 + "1"]  # 64 位 hex，含长数字段
        r = decision_gate.evaluate(p, POLICY)
        self.assertTrue(r["would_allow"], r["violations"])

    def test_amount_below_min(self):
        p = base_payload(); p["proposal"]["amount"] = 5.0
        self._expect_violation(p, "amount_below_min")

    def test_etf_symbol_whitelist(self):
        p = base_payload()
        p["proposal"].update({"symbol": "SPYU", "asset_class": "etf",
                              "trigger": "weight_deviation"})
        p["proposal"]["declares"] = {"is_leveraged_or_inverse": False, "is_otc": False}
        self._expect_violation(p, "etf_symbol_not_whitelisted")

    def test_cash_equiv_symbol_whitelist(self):
        p = base_payload()
        p["proposal"].update({"symbol": "ABCD", "asset_class": "cash_equiv",
                              "trigger": "weight_deviation"})
        p["proposal"]["declares"] = {"is_leveraged_or_inverse": False, "is_otc": False}
        self._expect_violation(p, "cash_equiv_symbol_not_whitelisted")

    def test_declares_must_be_dict(self):
        p = base_payload(); p["proposal"]["declares"] = ["not", "a", "dict"]
        self._expect_violation(p, "invalid_declares")

    def test_no_execution_fields_ever(self):
        r = decision_gate.evaluate(base_payload(), POLICY)
        s = json.dumps(r)
        for word in ("can_submit", "can_preview", "place_order"):
            self.assertNotIn(word, s)
        self.assertEqual(r["execution_capability"], "none")


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.log = str(Path(self.dir.name) / "journal.jsonl")

    def tearDown(self):
        self.dir.cleanup()

    def test_append_and_chain(self):
        e1 = journal_append.append(self.log, {"type": "card", "symbol": "VTI"})
        e2 = journal_append.append(self.log, {"type": "fill", "symbol": "VTI"})
        self.assertEqual(e1["seq"], 0)
        self.assertEqual(e2["prev_hash"], e1["hash"])

    def test_tamper_detected(self):
        journal_append.append(self.log, {"type": "card", "n": 1})
        journal_append.append(self.log, {"type": "card", "n": 2})
        lines = Path(self.log).read_text().splitlines()
        bad = json.loads(lines[0]); bad["event"]["n"] = 99
        Path(self.log).write_text(json.dumps(bad) + "\n" + lines[1] + "\n")
        with self.assertRaises(ValueError):
            journal_append.append(self.log, {"type": "card", "n": 3})

    def test_sensitive_key_rejected(self):
        with self.assertRaises(ValueError):
            journal_append.append(self.log, {"type": "fill", "api_key": "x"})

    def test_account_like_value_rejected(self):
        with self.assertRaises(ValueError):
            journal_append.append(self.log, {"type": "fill", "memo": "acct 987654321012"})

    def test_masked_last4_ok(self):
        entry = journal_append.append(self.log, {"type": "fill", "memo": "****1234 已核对"})
        self.assertEqual(entry["seq"], 0)

    def test_grouped_digits_rejected(self):
        with self.assertRaises(ValueError):
            journal_append.append(self.log, {"type": "fill", "memo": "acct 9876 5432 1012"})

    def test_decision_key_hex_allowed(self):
        entry = journal_append.append(self.log, {"type": "fill", "decision_key": "a" * 63 + "1"})
        self.assertEqual(entry["seq"], 0)


class BehaviorTests(unittest.TestCase):
    def _write(self, events):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        log = str(Path(d.name) / "j.jsonl")
        for e in events:
            journal_append.append(log, e)
        return log

    def test_disposition_effect_flagged(self):
        events = []
        for i in range(3):
            events.append({"type": "fill", "date_et": f"2026-08-0{i+1}", "symbol": "A",
                           "side": "sell", "amount": 100.0, "pnl": 10.0, "holding_days": 2})
        for i in range(3):
            events.append({"type": "fill", "date_et": f"2026-08-0{i+4}", "symbol": "B",
                           "side": "sell", "amount": 100.0, "pnl": -10.0, "holding_days": 20})
        fills = behavior_review.load_fills(self._write(events))
        findings = {f["bias"]: f for f in behavior_review.analyze(fills, 6)}
        self.assertTrue(findings["disposition_effect"]["flag"])

    def test_insufficient_data_honest(self):
        fills = behavior_review.load_fills(self._write(
            [{"type": "fill", "date_et": "2026-08-01", "symbol": "A", "side": "buy",
              "amount": 100.0}]))
        findings = {f["bias"]: f for f in behavior_review.analyze(fills, 6)}
        self.assertEqual(findings["disposition_effect"]["stats"].get("note"), "insufficient_data")
        self.assertFalse(findings["disposition_effect"]["flag"])

    def test_chasing_flagged(self):
        events = [{"type": "fill", "date_et": f"2026-08-0{i+1}", "symbol": "A", "side": "buy",
                   "amount": 100.0, "run_up_5d_pct": 15.0} for i in range(5)]
        fills = behavior_review.load_fills(self._write(events))
        findings = {f["bias"]: f for f in behavior_review.analyze(fills, 6)}
        self.assertTrue(findings["chasing"]["flag"])

    def test_deviation_ratio(self):
        events = [
            {"type": "fill", "date_et": "2026-08-01", "symbol": "A", "side": "buy",
             "amount": 100.0, "deviation": "none"},
            {"type": "fill", "date_et": "2026-08-02", "symbol": "A", "side": "buy",
             "amount": 100.0, "deviation": "改了金额"},
        ]
        fills = behavior_review.load_fills(self._write(events))
        findings = {f["bias"]: f for f in behavior_review.analyze(fills, 6)}
        self.assertEqual(findings["card_deviation"]["stats"]["deviations"], 1)
        self.assertTrue(findings["card_deviation"]["flag"])  # 0.5 > 0.3


if __name__ == "__main__":
    unittest.main(verbosity=1)
