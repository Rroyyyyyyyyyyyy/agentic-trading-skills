#!/usr/bin/env python3
"""Mandate, reconciliation and Robinhood review-contract tests. No broker access."""
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import live_gate
import order_ledger
import runtime_scope_gate_live
from test_desk import base_payload

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 13, 15, 42, tzinfo=ET)


def make_env():
    tmp = tempfile.TemporaryDirectory()
    policy = json.loads(json.dumps(live_gate.load_policy()))
    policy["mandate_file"] = str(Path(tmp.name) / "mandate.json")
    policy["halt_file"] = str(Path(tmp.name) / "HALT")
    policy["gate_state_file"] = str(Path(tmp.name) / "gate-state.json")
    return tmp, policy


def write_mandate(policy, *, start=NOW - timedelta(hours=1), days=7,
                  max_order=600.0, max_turnover=1500.0, max_orders=3,
                  last4=None, policy_hash=None, toolset_hash=None, perms=0o600):
    toolset = json.loads(live_gate.TOOLSET_PATH.read_text())
    account_last4 = last4 or policy["account_last4"]
    mandate = {
        "mandate_version": "2.0", "mandate_id": str(uuid.uuid4()),
        "account_ref_masked": f"****{account_last4}", "execution_mode": "supervised_review",
        "requires_per_order_confirmation": True,
        "issued_at_et": start.isoformat(), "not_before_et": start.isoformat(),
        "expires_at_et": (start + timedelta(days=days)).isoformat(),
        "max_order_amount": max_order, "max_daily_turnover": max_turnover,
        "max_orders_per_day": max_orders,
        "policy_sha256": policy_hash or live_gate._canonical_hash(policy),
        "toolset_sha256": toolset_hash or live_gate._canonical_hash(toolset),
        "confirmation": live_gate.MANDATE_CONFIRMATION_TEMPLATE.format(last4=account_last4),
    }
    path = Path(policy["mandate_file"])
    path.write_text(json.dumps(mandate)); os.chmod(path, perms)
    return mandate


def payload(phase="pre_review"):
    row = base_payload()
    row.pop("mode")
    row["phase"] = phase
    row["account"]["coverage"] = {
        "positions_complete": True, "open_equity_orders_complete": True,
        "today_equity_orders_complete": True, "today_fills_complete": True,
        "advanced_orders_checked": True,
    }
    row["reconciliation"] = {"local_intents": [], "broker_orders": []}
    row["review"] = None
    return row


def review(symbol="ABCD", side="buy", amount="500.00", observed=None):
    return {
        "observed_at_et": (observed or (NOW - timedelta(seconds=30))).isoformat(),
        "request": {"symbol": symbol, "side": side, "type": "market",
                    "dollar_amount": amount, "market_hours": "regular_hours",
                    "time_in_force": "gfd"},
        "response": {"symbol": symbol, "side": side, "type": "market",
                     "dollar_amount": amount, "order_checks": {},
                     "market_data_disclosure": "Bid $25.00 · Ask $25.02. Updated 3:41 PM ET.",
                     "quote_data": {"symbol": symbol, "state": "active",
                                    "has_traded": True, "bid_price": "25.00",
                                    "ask_price": "25.02"}},
    }


def evaluate(row, policy, state=None, now=NOW):
    return live_gate.evaluate(row, policy, now=now, skip_keychain=True,
                              state=state or {"date_et": "2026-08-13", "orders": []})


class MandateTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.policy = make_env()
        self.addCleanup(self.tmp.cleanup)

    def test_missing_is_shadow(self):
        result = evaluate(payload(), self.policy)
        self.assertEqual(result["mode"], "shadow")
        self.assertTrue(result["shadow_would_allow"], result["violations"])
        self.assertFalse(result["can_review"])

    def test_valid_is_supervised(self):
        write_mandate(self.policy)
        result = evaluate(payload(), self.policy)
        self.assertEqual(result["mode"], "supervised")
        self.assertTrue(result["can_review"], result["violations"])
        self.assertFalse(result["can_submit"])

    def test_wrong_account(self):
        write_mandate(self.policy, last4="9999")
        result = evaluate(payload(), self.policy)
        self.assertEqual(result["mandate_status"], "mandate_account_mismatch")

    def test_expired(self):
        write_mandate(self.policy, start=NOW - timedelta(days=8), days=7)
        self.assertEqual(evaluate(payload(), self.policy)["mandate_status"],
                         "mandate_expired")

    def test_policy_drift(self):
        write_mandate(self.policy, policy_hash="0" * 64)
        self.assertEqual(evaluate(payload(), self.policy)["mandate_status"],
                         "mandate_policy_drift")

    def test_toolset_drift(self):
        write_mandate(self.policy, toolset_hash="0" * 64)
        self.assertEqual(evaluate(payload(), self.policy)["mandate_status"],
                         "mandate_toolset_drift")

    def test_open_permissions(self):
        write_mandate(self.policy, perms=0o644)
        self.assertEqual(evaluate(payload(), self.policy)["mandate_status"],
                         "mandate_permissions_too_open")

    def test_mandate_order_cap(self):
        write_mandate(self.policy, max_order=400)
        self.assertIn("mandate_order_amount_exceeded",
                      evaluate(payload(), self.policy)["violations"])

    def test_mandate_count_cap(self):
        write_mandate(self.policy, max_orders=1)
        self.assertIn("mandate_daily_order_limit_reached",
                      evaluate(payload(), self.policy)["violations"])

    def test_mandate_never_grants_submit(self):
        write_mandate(self.policy)
        row = payload("post_review"); row["review"] = review()
        result = evaluate(row, self.policy)
        self.assertTrue(result["can_request_confirmation"], result["violations"])
        self.assertFalse(result["can_submit"])


class RuntimeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.policy = make_env(); self.addCleanup(self.tmp.cleanup)
        write_mandate(self.policy)

    def test_halt_blocks_buy(self):
        Path(self.policy["halt_file"]).touch()
        self.assertIn("halt_active", evaluate(payload(), self.policy)["violations"])

    def test_halt_allows_risk_exit(self):
        Path(self.policy["halt_file"]).touch()
        row = payload()
        row["account"]["positions"].append({"symbol": "ABCD", "value": 600,
            "sellable_value": 600, "asset_class": "stock"})
        row["proposal"].update(side="sell", trigger="drawdown_action")
        row["proposal"]["declares"] = {"is_leveraged_or_inverse": False,
                                         "is_otc": False}
        self.assertTrue(evaluate(row, self.policy)["can_review"])

    def test_missing_coverage(self):
        row = payload(); row["account"].pop("coverage")
        self.assertIn("snapshot_coverage_missing", evaluate(row, self.policy)["violations"])

    def test_advanced_orders_unverified(self):
        row = payload(); row["account"]["coverage"]["advanced_orders_checked"] = False
        self.assertIn("advanced_order_coverage_unverified",
                      evaluate(row, self.policy)["violations"])

    def test_weekend(self):
        weekend = datetime(2026, 8, 15, 15, 42, tzinfo=ET)
        row = payload(); row["as_of_et"] = "2026-08-15T15:40:00-04:00"
        self.assertIn("not_weekday_session", evaluate(row, self.policy, now=weekend)["violations"])

    def test_outside_hours(self):
        early = datetime(2026, 8, 13, 9, 0, tzinfo=ET)
        row = payload(); row["as_of_et"] = "2026-08-13T08:58:00-04:00"
        self.assertIn("outside_regular_session", evaluate(row, self.policy, now=early)["violations"])

    def test_new_stock_before_ten(self):
        early = datetime(2026, 8, 13, 9, 45, tzinfo=ET)
        row = payload(); row["as_of_et"] = "2026-08-13T09:43:00-04:00"
        self.assertIn("new_stock_risk_too_early", evaluate(row, self.policy, now=early)["violations"])

    def test_deep_drawdown_requests_halt(self):
        row = payload(); row["account"]["equity"] = 3300
        result = evaluate(row, self.policy)
        self.assertTrue(result["halt_required"])


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.policy = make_env(); self.addCleanup(self.tmp.cleanup)
        write_mandate(self.policy)

    def test_matched_active(self):
        row = payload(); row["reconciliation"] = {
            "local_intents": [{"order_id": "o1", "status": "queued"}],
            "broker_orders": [{"order_id": "o1", "status": "queued",
                               "placed_agent": "agentic"}]}
        self.assertTrue(evaluate(row, self.policy)["can_review"])

    def test_unmatched_active(self):
        row = payload(); row["reconciliation"] = {
            "local_intents": [{"order_id": "o1", "status": "queued"}],
            "broker_orders": []}
        self.assertIn("reconciliation_mismatch", evaluate(row, self.policy)["violations"])

    def test_external_open_order(self):
        row = payload(); row["reconciliation"] = {"local_intents": [],
            "broker_orders": [{"order_id": "o1", "status": "queued",
                               "placed_agent": "user"}]}
        self.assertIn("external_open_order_present", evaluate(row, self.policy)["violations"])

    def test_unknown_status(self):
        row = payload(); row["reconciliation"] = {
            "local_intents": [{"order_id": "o1", "status": "unknown"}],
            "broker_orders": [{"order_id": "o1", "status": "unknown",
                               "placed_agent": "agentic"}]}
        self.assertIn("reconciliation_mismatch", evaluate(row, self.policy)["violations"])

    def test_terminal_absent_ok(self):
        row = payload(); row["reconciliation"] = {
            "local_intents": [{"order_id": "o1", "status": "filled"}],
            "broker_orders": []}
        self.assertTrue(evaluate(row, self.policy)["can_review"])


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.policy = make_env(); self.addCleanup(self.tmp.cleanup)
        write_mandate(self.policy)

    def run_review(self, value, state=None):
        row = payload("post_review"); row["review"] = value
        return evaluate(row, self.policy, state=state)

    def test_clean_requests_confirmation(self):
        result = self.run_review(review())
        self.assertTrue(result["can_request_confirmation"], result["violations"])
        self.assertTrue(result["approved_intent"]["requires_explicit_confirmation"])
        uuid.UUID(result["approved_intent"]["ref_id"])

    def test_missing(self):
        self.assertIn("missing_review", self.run_review(None)["violations"])

    def test_alert(self):
        value = review(); value["response"]["order_checks"] = {"alert_type": "TEST"}
        self.assertIn("review_has_alerts", self.run_review(value)["violations"])

    def test_symbol_mismatch(self):
        self.assertIn("review_proposal_mismatch",
                      self.run_review(review(symbol="VTI"))["violations"])

    def test_amount_mismatch(self):
        self.assertIn("review_proposal_mismatch",
                      self.run_review(review(amount="400.00"))["violations"])

    def test_missing_disclosure(self):
        value = review(); value["response"]["market_data_disclosure"] = ""
        self.assertIn("review_disclosure_missing", self.run_review(value)["violations"])

    def test_wide_spread(self):
        value = review(); value["response"]["quote_data"].update(
            bid_price="24.00", ask_price="26.00")
        self.assertIn("review_spread_too_wide", self.run_review(value)["violations"])

    def test_stale(self):
        value = review(observed=NOW - timedelta(minutes=3))
        self.assertIn("review_stale", self.run_review(value)["violations"])

    def test_replay(self):
        value = review(); fingerprint = live_gate._review_fingerprint(value)
        state = {"date_et": "2026-08-13", "orders": [{
            "decision_key": "k", "review_fingerprint": fingerprint,
            "ref_id": str(uuid.uuid4()), "amount": 50.0}]}
        self.assertIn("review_replayed", self.run_review(value, state)["violations"])

    def test_limit_review(self):
        row = payload("post_review")
        row["proposal"].update(order_type="limit_day", quantity=20.0,
                               limit_price=25.0, amount=500.0)
        value = review()
        value["request"].pop("dollar_amount"); value["response"].pop("dollar_amount")
        value["request"].update(type="limit", quantity="20", limit_price="25")
        value["response"].update(type="limit", quantity="20", limit_price="25")
        row["review"] = value
        self.assertTrue(evaluate(row, self.policy)["can_request_confirmation"])


class GateStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp, self.policy = make_env(); self.addCleanup(self.tmp.cleanup)
        write_mandate(self.policy, max_orders=3)

    def test_state_is_stricter_than_caller(self):
        state = {"date_et": "2026-08-13", "orders": [
            {"decision_key": f"k{i}", "review_fingerprint": f"r{i}",
             "ref_id": str(uuid.uuid4()), "amount": 50.0} for i in range(3)]}
        self.assertIn("mandate_daily_order_limit_reached",
                      evaluate(payload(), self.policy, state=state)["violations"])

    def test_state_file_permissions(self):
        state = {"date_et": "2026-08-13", "orders": []}
        live_gate.save_state(self.policy["gate_state_file"], state)
        self.assertEqual(Path(self.policy["gate_state_file"]).stat().st_mode & 0o777, 0o600)


class OrderLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / "orders.json")
        self.ref_id = str(uuid.uuid4())

    def event(self, **updates):
        row = {"decision_key": "a" * 64, "ref_id": self.ref_id,
               "order_id": "broker-1", "symbol": "VTI", "side": "buy",
               "asset_class": "etf", "amount": 100.0, "status": "queued",
               "observed_at_et": NOW.isoformat()}
        row.update(updates); return row

    def test_record_and_project(self):
        order_ledger.record_submission(self.path, self.event())
        self.assertEqual(order_ledger.read_projection(self.path),
                         [{"order_id": "broker-1", "status": "queued"}])
        self.assertEqual(Path(self.path).stat().st_mode & 0o777, 0o600)

    def test_unknown_then_bind(self):
        order_ledger.record_submission(self.path, self.event(
            order_id=None, status="submission_unknown"))
        order_ledger.update_status(self.path, {"ref_id": self.ref_id,
            "order_id": "broker-1", "status": "filled",
            "observed_at_et": (NOW + timedelta(minutes=1)).isoformat()})
        self.assertEqual(order_ledger.read_projection(self.path)[0]["status"], "filled")

    def test_duplicate_ref(self):
        order_ledger.record_submission(self.path, self.event())
        with self.assertRaises(ValueError):
            order_ledger.record_submission(self.path, self.event())

    def test_terminal_immutable(self):
        order_ledger.record_submission(self.path, self.event(status="filled"))
        with self.assertRaises(ValueError):
            order_ledger.update_status(self.path, {"ref_id": self.ref_id,
                "order_id": "broker-1", "status": "queued",
                "observed_at_et": (NOW + timedelta(minutes=1)).isoformat()})


class SkillContractTests(unittest.TestCase):
    def test_shadow_uses_zero_argument_account_bound_snapshot(self):
        skill = (Path(__file__).parents[1] / "SKILL.md").read_text()
        self.assertIn("robinhood-account-readonly", skill)
        self.assertIn("get_strategy_snapshot", skill)
        self.assertIn("不得传任何参数", skill)
        self.assertIn("trade_readiness=false", skill)
        self.assertIn("不得改用 `get_accounts`", skill)


class RuntimeScopeTests(unittest.TestCase):
    def config(self, tools=None):
        expected = json.loads(live_gate.TOOLSET_PATH.read_text())["expected_tools"]
        return {"name": "robinhood-trading", "enabled": True,
                "transport": {"type": "streamable_http",
                              "url": "https://agent.robinhood.com/mcp/trading"},
                "enabled_tools": tools or expected}

    def test_exact_scope(self):
        expected = set(json.loads(live_gate.TOOLSET_PATH.read_text())["expected_tools"])
        result = runtime_scope_gate_live.evaluate_scope(self.config(), expected)
        self.assertTrue(result["scope_pass"])
        self.assertFalse(result["autonomous_execution_authorized"])

    def test_extra_tool(self):
        expected = set(json.loads(live_gate.TOOLSET_PATH.read_text())["expected_tools"])
        result = runtime_scope_gate_live.evaluate_scope(
            self.config(sorted(expected | {"get_accounts"})), expected)
        self.assertFalse(result["scope_pass"])

    def test_duplicate_tool(self):
        expected = list(json.loads(live_gate.TOOLSET_PATH.read_text())["expected_tools"])
        result = runtime_scope_gate_live.evaluate_scope(self.config(expected + [expected[0]]),
                                                         set(expected))
        self.assertFalse(result["scope_pass"])

    def test_wrong_url(self):
        expected = set(json.loads(live_gate.TOOLSET_PATH.read_text())["expected_tools"])
        cfg = self.config(); cfg["transport"]["url"] = "https://example.invalid"
        self.assertFalse(runtime_scope_gate_live.evaluate_scope(cfg, expected)["scope_pass"])


if __name__ == "__main__":
    unittest.main(verbosity=1)
