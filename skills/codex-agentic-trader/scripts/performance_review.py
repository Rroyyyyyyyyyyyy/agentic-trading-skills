#!/usr/bin/env python3
"""以同区间、净现金流调整且扣成本的收益比较策略与 VOO/QQQ。"""
import argparse
import json
import math
import sys
from pathlib import Path

POLICY_PATH = Path(__file__).parents[1] / "policy" / "policy.json"


def load_policy():
    return json.loads(POLICY_PATH.read_text())


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def evaluate(payload, policy=None):
    policy = policy or load_policy()
    periods = payload.get("periods")
    if not isinstance(periods, list) or not periods:
        raise ValueError("periods_nonempty_list_required")
    objective = policy["performance_objective"]
    rows = []
    expand = False
    for row in periods:
        if not isinstance(row, dict):
            raise ValueError("invalid_period")
        label = row.get("label")
        days = row.get("trading_days")
        if not isinstance(label, str) or not label:
            raise ValueError("period_label_required")
        if not isinstance(days, int) or isinstance(days, bool) or days <= 0:
            raise ValueError("trading_days_positive_integer_required")
        for key in ("strategy_return", "voo_return", "qqq_return"):
            if not _finite(row.get(key)) or row[key] <= -1:
                raise ValueError(f"invalid_{key}")
        for key in ("same_interval", "cash_flow_adjusted", "net_of_costs"):
            if row.get(key) is not True:
                raise ValueError(f"{key}_required")
        strategy = row["strategy_return"]
        gap_voo = strategy - row["voo_return"]
        gap_qqq = strategy - row["qqq_return"]
        beats_both = gap_voo > 0 and gap_qqq > 0
        can_claim = days >= objective["min_claim_trading_days"]
        escalate = days >= objective["underperformance_escalation_days"] and not beats_both
        expand = expand or escalate
        rows.append({
            "label": label,
            "trading_days": days,
            "gap_vs_voo": gap_voo,
            "gap_vs_qqq": gap_qqq,
            "outperformed_both": beats_both,
            "can_claim_outperformance": can_claim and beats_both,
            "status": ("insufficient_history" if not can_claim
                       else "outperforming_both" if beats_both
                       else "underperforming_one_or_both"),
        })
    return {
        "objective": "outperform_both_voo_and_qqq",
        "periods": rows,
        "expand_qualified_stock_search": expand,
        "trade_authorized": False,
        "note": ("落后只提高合格个股研究与候选优先级；任何实际订单仍须通过全部硬门，"
                 "不保证收益，也不因落后提高风险上限。"),
    }


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
