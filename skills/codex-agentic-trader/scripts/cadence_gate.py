#!/usr/bin/env python3
"""按美东时间和真实无杠杆购买力决定本轮工作强度。

本脚本只决定是否做完整分析/操作判断，不访问券商，也不授权交易。
"""
import argparse
import json
import math
import sys
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

POLICY_PATH = Path(__file__).parents[1] / "policy" / "policy.json"


def load_policy():
    return json.loads(POLICY_PATH.read_text())


def _parse_et(value, timezone):
    if not isinstance(value, str):
        raise ValueError("as_of_et_required")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("as_of_et_timezone_required")
    return dt.astimezone(ZoneInfo(timezone))


def _finite_nonnegative(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value >= 0


def evaluate(payload, policy=None):
    policy = policy or load_policy()
    now = _parse_et(payload.get("as_of_et"), policy["market_timezone"])
    is_trading_day = payload.get("is_trading_day")
    urgent = payload.get("urgent_risk_event", False)
    if not isinstance(is_trading_day, bool):
        raise ValueError("is_trading_day_boolean_required")
    if not isinstance(urgent, bool):
        raise ValueError("urgent_risk_event_boolean_required")

    bp = payload.get("unleveraged_buying_power")
    bp_verified = _finite_nonnegative(bp)
    threshold = policy["min_order_amount"] + policy["min_cash_buffer"]
    sufficient = bp_verified and bp >= threshold
    hm = (now.hour, now.minute)
    result = {
        "as_of_et": now.isoformat(),
        "action": "none",
        "full_analysis": False,
        "operation_decision": False,
        "trade_permitted_by_cadence": False,
        "send_daily_report": False,
        "run_evolution": False,
        "buying_power_verified": bp_verified,
        "buying_power_sufficient": sufficient,
        "buying_power_threshold": threshold,
        "reason": "outside_scheduled_work",
    }

    # 每天固定两次完整分析；休市日也保留市场/新闻扫描和收盘日报。
    if hm == (8, 30):
        result.update(action="full_preopen_analysis", full_analysis=True,
                      reason="daily_preopen_analysis")
        return result
    if hm == (17, 30):
        result.update(action="full_close_analysis", full_analysis=True,
                      send_daily_report=True, run_evolution=True,
                      reason="daily_close_analysis_report_and_evolution")
        return result
    if hm == (8, 0):
        result.update(action="connection_calendar_check",
                      reason="daily_connection_check")
        return result
    if hm == (9, 0):
        result.update(action="material_news_supplement",
                      reason="preopen_news_supplement_only")
        return result

    if not is_trading_day:
        result["reason"] = "market_closed"
        return result

    if hm in {(16, 30), (17, 0)}:
        result.update(action="order_reconciliation",
                      reason="post_close_reconciliation_only")
        return result

    in_regular_window = time(9, 30) <= now.time().replace(tzinfo=None) <= time(16, 0)
    on_half_hour = now.minute in (0, 30)
    if not (in_regular_window and on_half_hour):
        return result

    if urgent:
        result.update(action="operation_decision", operation_decision=True,
                      trade_permitted_by_cadence=bp_verified,
                      reason="urgent_risk_or_order_event")
        return result

    if sufficient:
        result.update(action="operation_decision", operation_decision=True,
                      trade_permitted_by_cadence=True,
                      reason="sufficient_buying_power_30m_cadence")
        return result

    two_hour_slot = now.minute == 0 and now.hour in (10, 12, 14, 16)
    if two_hour_slot:
        result.update(action="operation_decision", operation_decision=True,
                      trade_permitted_by_cadence=bp_verified,
                      reason=("insufficient_buying_power_2h_cadence"
                              if bp_verified else "buying_power_unverified_fail_closed"))
        return result

    result.update(action="account_order_probe",
                  reason="insufficient_buying_power_between_2h_decisions")
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="JSON 文件；缺省读 stdin")
    args = ap.parse_args()
    try:
        raw = Path(args.input).read_text() if args.input else sys.stdin.read()
        print(json.dumps(evaluate(json.loads(raw)), ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}:{exc}"},
                         ensure_ascii=False))
        sys.exit(1)


if __name__ == "__main__":
    main()
