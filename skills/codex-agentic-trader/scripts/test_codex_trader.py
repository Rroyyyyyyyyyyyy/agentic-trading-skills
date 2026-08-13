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


def good_preview(symbol="ABCD", side="buy", amount=500.0):
    return {"preview_id": "pv-1", "preflight_status": "clean", "warnings": [],
            "quoted_price": 25.05, "preview_age_seconds": 30,
            "symbol": symbol, "side": side, "amount": amount}


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
    def test_halt_blocks_buy(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy)
        Path(policy["halt_file"]).touch()
        r = live_gate.evaluate(base_payload(), policy, today=TODAY, skip_keychain=True)
        self.assertIn("halt_active", r["violations"])
        self.assertFalse(r.get("can_preview"))

    def test_halt_allows_risk_exit_sell(self):
        # hard-boundaries §4：HALT 期间清仓走风险退出通道
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy)
        Path(policy["halt_file"]).touch()
        p = base_payload()
        p["account"]["positions"].append({"symbol": "ABCD", "value": 600.0, "asset_class": "stock"})
        p["proposal"].update({"side": "sell", "trigger": "drawdown_action"})
        p["proposal"]["declares"] = {"is_leveraged_or_inverse": False, "is_otc": False}
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertTrue(r["can_preview"], r["violations"])

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


class AdversarialFixTests(unittest.TestCase):
    """对抗审查 P1-P7 修复的回归。"""

    def _live_env(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy)
        return policy

    # P1: preview 与提案绑定 + 重放
    def test_preview_symbol_mismatch_rejected(self):
        policy = self._live_env()
        p = base_payload("post_preview")
        p["preview"] = good_preview(symbol="VTI")
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertIn("preview_proposal_mismatch", r["violations"])

    def test_preview_amount_mismatch_rejected(self):
        policy = self._live_env()
        p = base_payload("post_preview")
        p["preview"] = good_preview(amount=400.0)
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertIn("preview_proposal_mismatch", r["violations"])

    def test_preview_replay_rejected_via_history(self):
        policy = self._live_env()
        p = base_payload("post_preview")
        p["preview"] = good_preview()
        p["history"]["used_preview_ids"] = ["pv-1"]
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertIn("preview_replayed", r["violations"])

    # P2: 闸门状态账本比调用方声明更严
    def test_state_ledger_enforces_order_limit(self):
        policy = self._live_env()
        state = {"date_et": TODAY, "orders": [
            {"decision_key": f"k{i}", "preview_id": f"p{i}", "amount": 50.0}
            for i in range(6)]}
        p = base_payload()  # 调用方谎报 orders_today=1
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True, state=state)
        self.assertIn("daily_order_limit_reached", r["violations"])

    def test_state_ledger_enforces_turnover(self):
        policy = self._live_env()
        state = {"date_et": TODAY, "orders": [
            {"decision_key": "k1", "preview_id": "p1", "amount": 1400.0}]}
        p = base_payload()  # 调用方谎报 turnover_today=300；账本 1400+500 > 1500
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True, state=state)
        self.assertIn("daily_turnover_over_cap", r["violations"])

    def test_state_ledger_enforces_idempotency(self):
        policy = self._live_env()
        import decision_gate
        key = decision_gate.compute_decision_key(TODAY, "ABCD", "buy",
                                                 "qualified_candidate", 500.0)
        state = {"date_et": TODAY, "orders": [
            {"decision_key": key, "preview_id": "px", "amount": 500.0}]}
        r = live_gate.evaluate(base_payload(), policy, today=TODAY,
                               skip_keychain=True, state=state)
        self.assertIn("duplicate_decision", [v for v in r["violations"]])

    def test_state_ledger_blocks_preview_replay(self):
        policy = self._live_env()
        state = {"date_et": TODAY, "orders": [
            {"decision_key": "kold", "preview_id": "pv-1", "amount": 100.0}]}
        p = base_payload("post_preview"); p["preview"] = good_preview()
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True, state=state)
        self.assertIn("preview_replayed", r["violations"])

    # P3: 到期日真实解析
    def test_malformed_expiry_dates_all_shadow(self):
        for bad in ("2026-13-45", "9999-99-99", "2026-08-9 ", "2026-8-13 "):
            d, policy = make_env(); self.addCleanup(d.cleanup)
            write_mandate(policy, expires=bad)
            r = live_gate.evaluate(base_payload(), policy, today=TODAY, skip_keychain=True)
            self.assertEqual(r["mode"], "shadow", bad)
            self.assertEqual(r["mandate_status"], "mandate_expired", bad)

    def test_expiry_today_still_valid(self):
        d, policy = make_env(); self.addCleanup(d.cleanup)
        write_mandate(policy, expires=TODAY)
        r = live_gate.evaluate(base_payload(), policy, today=TODAY, skip_keychain=True)
        self.assertEqual(r["mode"], "live")

    # P4: limit_day 限价
    def test_limit_day_requires_limit_price(self):
        policy = self._live_env()
        p = base_payload(); p["proposal"]["order_type"] = "limit_day"
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertIn("limit_price_required", r["violations"])

    def test_market_day_rejects_limit_price(self):
        policy = self._live_env()
        p = base_payload(); p["proposal"]["limit_price"] = 25.0
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertIn("limit_price_forbidden_for_market_order", r["violations"])

    def test_approved_intent_locks_order(self):
        policy = self._live_env()
        p = base_payload("post_preview")
        p["proposal"]["order_type"] = "limit_day"
        p["proposal"]["limit_price"] = 24.9
        p["preview"] = good_preview()
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertTrue(r["can_submit"], r["violations"])
        ai = r["approved_intent"]
        self.assertEqual(ai["limit_price"], 24.9)
        self.assertEqual(ai["preview_id"], "pv-1")
        self.assertEqual(ai["decision_key"], r["decision_key"])
        self.assertEqual(ai["asset_class"], "stock")

    # P6: preview/reconciliation 子树的账号扫描
    def test_account_digits_in_preview_scanned(self):
        policy = self._live_env()
        p = base_payload("post_preview")
        p["preview"] = good_preview()
        p["preview"]["memo"] = "acct 1234567890123456"
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertIn("account_like_identifier_present", r["violations"])

    # P7: broker 同 client_key 冲突状态
    def test_broker_conflicting_duplicate_client_key(self):
        policy = self._live_env()
        p = base_payload()
        p["reconciliation"] = {
            "local_intents": [{"client_key": "k1", "status": "open"}],
            "broker_orders": [{"client_key": "k1", "status": "open"},
                               {"client_key": "k1", "status": "filled"}]}
        r = live_gate.evaluate(p, policy, today=TODAY, skip_keychain=True)
        self.assertIn("reconciliation_mismatch", r["violations"])


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
