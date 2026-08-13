#!/usr/bin/env python3
"""codex-agentic-trader 回归测试：live_gate 两阶段 / mandate / HALT / 对账。

decision_gate 的基础检查已由 roy-trading-desk 的 test_desk.py 覆盖，此处只测扩展层。
测试全程 skip_keychain=True（Keychain 漂移检测在 macOS 真机由 mandate_admin 流程覆盖）。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import live_gate

TODAY = "2026-08-13"


def make_env():
    """独立临时目录的 policy（mandate/halt 路径指向 tmp）。"""
    d = tempfile.TemporaryDirectory()
    policy = live_gate.load_policy()
    policy = json.loads(json.dumps(policy))
    policy["mandate_file"] = str(Path(d.name) / "mandate.json")
    policy["halt_file"] = str(Path(d.name) / "HALT")
    return d, policy


def write_mandate(policy, expires="2026-09-12", max_order=600.0, max_turnover=1500.0,
                  last4=None, confirmation=None, perms=0o600):
    m = {
        "mandate_version": "1.0",
        "account_last4": last4 or policy["account_last4"],
        "issued_at_et": TODAY,
        "expires_at_et": expires,
        "max_order_amount": max_order,
        "max_daily_turnover": max_turnover,
        "confirmation": confirmation
        or f"I AUTHORIZE LIVE TRADING {last4 or policy['account_last4']}",
    }
    p = Path(policy["mandate_file"])
    p.write_text(json.dumps(m))
    os.chmod(p, perms)
    return m


def base_payload(phase="pre_preview"):
    return {
        "schema_version": "1.0",
        "phase": phase,
        "as_of_et": f"{TODAY}T15:40:00-04:00",
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
        "reconciliation": {"local_intents": [], "broker_orders": []},
        "preview": None,
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
                "price": 25.0, "market_cap": 2e10, "avg_daily_volume": 2e6,
                "above_50dma": True, "above_200dma": True,
                "ret_3m_positive": True, "ret_6m_positive": True,
                "rel_strength_vs_benchmark_20d": True, "days_to_earnings": 10,
                "no_thesis_breaking_news": True,
                "is_leveraged_or_inverse": False, "is_otc": False,
            },
        },
    }


def good_preview():
    return {"preview_id": "pv-1", "preflight_status": "clean", "warnings": [],
            "quoted_price": 25.05, "preview_age_seconds": 30}


class ModeDerivationTests(unittest.TestCase):
    def test_no_mandate_falls_to_shadow(self):
        d, policy = make_env()
        self.addCleanup(d.cleanup)
        r = live_gate.evaluate(base_payload(), policy, today=TODAY, skip_keychain=True)
        self.assertEqual(r["mode"], "shadow")
        self.assertEqual(r["mandate_status"], "mandate_missing")
        self.assertFalse(r["can_preview"]); self.assertFalse(r["can_submit"])
        self.assertTrue(r["shadow_would_allow"], r["violations"])

    def test_valid_mandate_live_pre_preview(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy)
        r = live_gate.evaluate(base_payload(), policy, today=TODAY, skip_keychain=True)
        self.assertEqual(r["mode"], "live")
        self.assertTrue(r["can_preview"], r["violations"])
        self.assertFalse(r["can_submit"])

    def test_expired_mandate_shadow(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy, expires="2026-08-12")
        r = live_gate.evaluate(base_payload(), policy, today=TODAY, skip_keychain=True)
        self.assertEqual(r["mode"], "shadow")
        self.assertEqual(r["mandate_status"], "mandate_expired")

    def test_wrong_last4_shadow(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy, last4="9999",
                      confirmation="I AUTHORIZE LIVE TRADING 9999")
        r = live_gate.evaluate(base_payload(), policy, today=TODAY, skip_keychain=True)
        self.assertEqual(r["mandate_status"], "mandate_account_mismatch")

    def test_open_permissions_shadow(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy, perms=0o644)
        r = live_gate.evaluate(base_payload(), policy, today=TODAY, skip_keychain=True)
        self.assertEqual(r["mandate_status"], "mandate_permissions_too_open")

    def test_keychain_mismatch_shadow(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy)
        # 不跳过 Keychain：测试环境没有对应条目 → checksum mismatch → shadow
        r = live_gate.evaluate(base_payload(), policy, today=TODAY, skip_keychain=False)
        self.assertEqual(r["mode"], "shadow")
        self.assertEqual(r["mandate_status"], "mandate_checksum_mismatch")

    def test_declared_mode_rejected(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy)
        p = base_payload(); p["mode"] = "live"
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertFalse(r["would_allow"])
        self.assertIn("mode_must_not_be_declared", r["violations"])


class HaltTests(unittest.TestCase):
    def test_halt_blocks_live(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy)
        Path(policy["halt_file"]).touch()
        r = live_gate.evaluate(base_payload(), policy, today=TODAY, skip_keychain=True)
        self.assertEqual(r["mode"], "shadow")
        self.assertIn("halt_active", r["violations"])
        self.assertFalse(r.get("can_preview"))

    def test_deep_drawdown_sets_halt_required(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy)
        p = base_payload()
        p["account"]["equity"] = 3300.0  # dd≈36.5% ≥ 35%
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertTrue(r["halt_required"])
        self.assertFalse(r["would_allow"])  # 敞口超 0% cap 被拒


class MandateCapTests(unittest.TestCase):
    def test_order_amount_over_mandate(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy, max_order=400.0)
        r = live_gate.evaluate(base_payload(), policy, today=TODAY, skip_keychain=True)
        self.assertIn("mandate_order_amount_exceeded", r["violations"])

    def test_daily_turnover_over_mandate(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy, max_turnover=700.0)  # 300 已用 + 500 > 700
        r = live_gate.evaluate(base_payload(), policy, today=TODAY, skip_keychain=True)
        self.assertIn("mandate_daily_turnover_exceeded", r["violations"])

    def test_risk_exit_sell_bypasses_mandate_turnover(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy, max_turnover=700.0)
        p = base_payload()
        p["account"]["positions"].append({"symbol": "ABCD", "value": 600.0, "asset_class": "stock"})
        p["proposal"].update({"side": "sell", "trigger": "thesis_break"})
        p["proposal"]["declares"] = {"is_leveraged_or_inverse": False, "is_otc": False}
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertTrue(r["can_preview"], r["violations"])


class RiskExitMandateBypassTests(unittest.TestCase):
    def test_risk_exit_sell_bypasses_mandate_order_amount(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy, max_order=100.0)  # 远小于清仓额
        p = base_payload()
        p["account"]["positions"].append({"symbol": "ABCD", "value": 600.0, "asset_class": "stock"})
        p["proposal"].update({"side": "sell", "trigger": "drawdown_action", "amount": 600.0})
        p["proposal"]["declares"] = {"is_leveraged_or_inverse": False, "is_otc": False}
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertTrue(r["can_preview"], r["violations"])

    def test_normal_buy_still_capped_by_mandate(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy, max_order=100.0)
        r = live_gate.evaluate(base_payload(), policy, today=TODAY, skip_keychain=True)
        self.assertIn("mandate_order_amount_exceeded", r["violations"])


class ReconciliationTests(unittest.TestCase):
    def _run(self, recon):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy)
        p = base_payload(); p["reconciliation"] = recon
        return live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)

    def test_matched_ok(self):
        r = self._run({"local_intents": [{"client_key": "k1", "status": "filled"}],
                       "broker_orders": [{"client_key": "k1", "status": "filled"}]})
        self.assertTrue(r["can_preview"], r["violations"])

    def test_unmatched_local_open_intent(self):
        r = self._run({"local_intents": [{"client_key": "k1", "status": "open"}],
                       "broker_orders": []})
        self.assertIn("reconciliation_mismatch", r["violations"])

    def test_unknown_broker_order(self):
        r = self._run({"local_intents": [],
                       "broker_orders": [{"client_key": "kx", "status": "open"}]})
        self.assertIn("reconciliation_mismatch", r["violations"])

    def test_unknown_status_blocks(self):
        r = self._run({"local_intents": [{"client_key": "k1", "status": "unknown"}],
                       "broker_orders": [{"client_key": "k1", "status": "unknown"}]})
        self.assertIn("reconciliation_mismatch", r["violations"])

    def test_missing_reconciliation_blocks(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy)
        p = base_payload(); del p["reconciliation"]
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertIn("missing_reconciliation", r["violations"])

    def test_terminal_local_intent_absent_from_broker_ok(self):
        # 历史已成交单不再出现在券商当日/未完成集合中，不算不平
        r = self._run({"local_intents": [{"client_key": "old", "status": "filled"}],
                       "broker_orders": []})
        self.assertTrue(r["can_preview"], r["violations"])


class TwoPhaseTests(unittest.TestCase):
    def _live_env(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy)
        return policy

    def test_post_preview_clean_allows_submit(self):
        policy = self._live_env()
        p = base_payload("post_preview"); p["preview"] = good_preview()
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertTrue(r["can_submit"], r["violations"])
        self.assertEqual(r["approved_intent"]["symbol"], "ABCD")
        self.assertEqual(r["approved_intent"]["amount"], 500.0)

    def test_post_preview_missing_preview(self):
        policy = self._live_env()
        p = base_payload("post_preview")
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertIn("missing_preview", r["violations"])

    def test_preview_warning_rejected(self):
        policy = self._live_env()
        p = base_payload("post_preview")
        p["preview"] = good_preview(); p["preview"]["warnings"] = ["pattern_day_trading"]
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertIn("preview_has_warnings", r["violations"])
        self.assertFalse(r["can_submit"])

    def test_preview_not_clean_rejected(self):
        policy = self._live_env()
        p = base_payload("post_preview")
        p["preview"] = good_preview(); p["preview"]["preflight_status"] = "needs_confirmation"
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertIn("preview_not_clean", r["violations"])

    def test_stale_preview_rejected(self):
        policy = self._live_env()
        p = base_payload("post_preview")
        p["preview"] = good_preview(); p["preview"]["preview_age_seconds"] = 300
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertIn("preview_stale", r["violations"])

    def test_pre_preview_never_submits(self):
        policy = self._live_env()
        p = base_payload("pre_preview"); p["preview"] = good_preview()
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertTrue(r["can_preview"])
        self.assertFalse(r["can_submit"])
        self.assertNotIn("approved_intent", r)

    def test_invalid_phase_rejected(self):
        policy = self._live_env()
        p = base_payload("submit")
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertIn("invalid_phase", r["violations"])

    def test_shadow_never_previews_even_clean(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)  # 无 mandate
        p = base_payload("post_preview"); p["preview"] = good_preview()
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertEqual(r["mode"], "shadow")
        self.assertFalse(r["can_submit"]); self.assertFalse(r["can_preview"])


class BaseChecksStillApplyTests(unittest.TestCase):
    def test_base_violation_propagates(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy)
        p = base_payload()
        p["proposal"]["declares"]["price"] = 3.0  # penny stock
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertFalse(r["would_allow"])
        self.assertTrue(any("price_below_min" in v for v in r["violations"]))


if __name__ == "__main__":
    unittest.main(verbosity=1)
