#!/usr/bin/env python3
"""比较策略与 VOO/QQQ，并计算100个日历日净利润目标进度。"""
import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

POLICY_PATH = Path(__file__).parents[1] / "policy" / "policy.json"


def load_policy():
    return json.loads(POLICY_PATH.read_text())


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _date(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field}_iso_date_required")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field}_iso_date_required") from exc


def evaluate_challenge(challenge, policy):
    if not isinstance(challenge, dict):
        raise ValueError("challenge_object_required")
    rules = policy["profit_challenge"]
    duration = rules["duration_calendar_days"]
    target = rules["target_net_profit"]
    required_capital = rules["required_external_capital"]
    as_of = _date(challenge.get("as_of_date_et"), "as_of_date_et")
    external_capital = challenge.get("strategy_external_capital")
    if not _finite(external_capital) or external_capital < 0:
        raise ValueError("invalid_strategy_external_capital")
    if external_capital > policy["capital_cap"]:
        raise ValueError("strategy_external_capital_above_cap")

    if challenge.get("started") is not True:
        if challenge.get("started") is not False:
            raise ValueError("challenge_started_boolean_required")
        ready = math.isclose(external_capital, required_capital,
                             rel_tol=0.0, abs_tol=0.01)
        return {
            "status": "ready_day" if ready else "preparation",
            "challenge_day": None,
            "duration_calendar_days": duration,
            "net_profit": None,
            "target_net_profit": target,
            "target_remaining": target,
            "progress_ratio": None,
            "goal_met": False,
            "start_rule": rules["start_rule"],
            "note": ("全部5,000美元策略本金已可用；下一交易日锁定基线并记为Day 1。"
                     if ready else
                     "策略本金未全部可用，仍是准备期，不计算小额本金百分比进度。"),
        }

    if not math.isclose(external_capital, required_capital,
                        rel_tol=0.0, abs_tol=0.01):
        raise ValueError("active_challenge_requires_full_external_capital")
    for key in ("cash_flow_adjusted", "net_of_costs", "baseline_locked",
                "start_rule_verified"):
        if challenge.get(key) is not True:
            raise ValueError(f"challenge_{key}_required")
    start = _date(challenge.get("start_date_et"), "start_date_et")
    if as_of < start:
        raise ValueError("challenge_as_of_before_start")
    start_equity = challenge.get("start_equity_adjusted")
    current_equity = challenge.get("current_equity_adjusted")
    peak_equity = challenge.get("peak_equity_adjusted")
    if not _finite(start_equity) or start_equity <= 0:
        raise ValueError("invalid_start_equity_adjusted")
    if not _finite(current_equity) or current_equity < 0:
        raise ValueError("invalid_current_equity_adjusted")
    if not _finite(peak_equity) or peak_equity <= 0 or peak_equity < current_equity:
        raise ValueError("invalid_peak_equity_adjusted")

    elapsed = (as_of - start).days + 1
    net_profit = current_equity - start_equity
    expected_profit = target * min(elapsed, duration) / duration
    pace_shortfall = expected_profit - net_profit
    pace_ratio = net_profit / expected_profit if expected_profit > 0 else None
    drawdown = max(0.0, 1.0 - current_equity / peak_equity)
    remaining = max(0.0, target - net_profit)
    goal_met = net_profit >= target
    within_window = elapsed <= duration
    if goal_met and within_window:
        status = "target_met"
    elif within_window:
        status = "in_progress"
    elif goal_met:
        status = "target_met_after_deadline"
    else:
        status = "ended_below_target"
    return {
        "status": status,
        "challenge_day": min(elapsed, duration),
        "elapsed_calendar_days": elapsed,
        "calendar_days_remaining": max(0, duration - elapsed),
        "duration_calendar_days": duration,
        "start_date_et": start.isoformat(),
        "as_of_date_et": as_of.isoformat(),
        "start_equity_adjusted": start_equity,
        "current_equity_adjusted": current_equity,
        "peak_equity_adjusted": peak_equity,
        "current_drawdown": drawdown,
        "target_equity_adjusted": start_equity + target,
        "net_profit": net_profit,
        "return_on_start_equity": net_profit / start_equity,
        "target_net_profit": target,
        "target_remaining": remaining,
        "progress_ratio": net_profit / target,
        "linear_pace_expected_profit": expected_profit,
        "pace_shortfall": pace_shortfall,
        "pace_attainment_ratio": pace_ratio,
        "goal_met": goal_met,
        "within_100_day_window": within_window,
        "trade_authorized": False,
        "note": ("100天/$2,500是绩效目标，不是每日配额或交易触发；"
                 "不得因进度落后改变任何风险上限。"),
    }


def evaluate_daily(daily):
    if not isinstance(daily, dict):
        raise ValueError("daily_object_required")
    _date(daily.get("date_et"), "daily_date_et")
    for key in ("strategy_return", "voo_return", "qqq_return"):
        if not _finite(daily.get(key)) or daily[key] <= -1:
            raise ValueError(f"invalid_daily_{key}")
    for key in ("same_session", "cash_flow_adjusted", "net_of_costs",
                "completed_session"):
        if daily.get(key) is not True:
            raise ValueError(f"daily_{key}_required")
    gap_voo = daily["strategy_return"] - daily["voo_return"]
    gap_qqq = daily["strategy_return"] - daily["qqq_return"]
    won = gap_voo > 0 and gap_qqq > 0
    return {
        "date_et": daily["date_et"],
        "strategy_return": daily["strategy_return"],
        "voo_return": daily["voo_return"],
        "qqq_return": daily["qqq_return"],
        "gap_vs_voo": gap_voo,
        "gap_vs_qqq": gap_qqq,
        "objective_met": won,
        "status": "won_both" if won else "lost_to_one_or_both",
        "trade_authorized": False,
        "note": ("每日目标按完成收盘后的同一交易日净收益评分；"
                 "未达标不能单独触发交易或证明次日应提高风险。"),
    }


def evaluate_risk_posture(challenge, rows, context, policy):
    if not isinstance(context, dict):
        raise ValueError("risk_context_object_required")
    for key in ("benchmark_a_pass", "benchmark_b_pass", "snapshot_complete",
                "no_material_risk_event", "halt_active"):
        if not isinstance(context.get(key), bool):
            raise ValueError(f"risk_context_{key}_boolean_required")
    volatility = context.get("volatility_regime")
    if volatility not in ("low", "normal", "high", "unknown"):
        raise ValueError("invalid_volatility_regime")

    rules = policy["controlled_offense"]
    allocation = (
        rules["target_qqqm_weight"] + rules["target_vti_weight"]
        + rules["target_total_stock_weight"] + rules["minimum_defensive_weight"])
    if (not all(_finite(rules[key]) and 0 <= rules[key] <= 1 for key in (
                "target_qqqm_weight", "target_vti_weight",
                "target_total_stock_weight", "target_single_stock_weight",
                "minimum_defensive_weight"))
            or abs(allocation - 1.0) > 1e-9
            or rules["target_total_stock_weight"] > policy["stocks_total_max_weight"]
            or rules["target_single_stock_weight"] > policy["single_stock_max_weight"]
            or rules["target_max_stock_positions"] > policy["max_stock_positions"]
            or (rules["target_qqqm_weight"] + rules["target_vti_weight"]
                + rules["target_total_stock_weight"]
                > policy["market_state_stock_caps"]["both_pass"] + 1e-9)):
        raise ValueError("controlled_offense_policy_exceeds_hard_caps")
    rolling_underperformance = any(
        row["trading_days"] >= rules["requires_rolling_underperformance_days"]
        and not row["outperformed_both"] for row in rows)
    pace_ratio = challenge.get("pace_attainment_ratio")
    far_behind = (
        challenge.get("status") == "in_progress"
        and challenge.get("elapsed_calendar_days", 0) >= rules["min_elapsed_calendar_days"]
        and _finite(challenge.get("pace_shortfall"))
        and challenge["pace_shortfall"] >= rules["min_pace_shortfall_dollars"]
        and _finite(pace_ratio)
        and pace_ratio < rules["max_pace_attainment_ratio_to_enter"]
    )
    safeguards = {
        "far_behind_linear_pace": far_behind,
        "rolling_underperformance": rolling_underperformance,
        "both_trends_pass": (context["benchmark_a_pass"]
                             and context["benchmark_b_pass"]),
        "drawdown_below_entry_limit": (
            _finite(challenge.get("current_drawdown"))
            and challenge["current_drawdown"] < rules["max_drawdown_to_enter"]),
        "volatility_allowed": volatility in rules["allowed_volatility_regimes"],
        "snapshot_complete": context["snapshot_complete"],
        "no_material_risk_event": context["no_material_risk_event"],
        "halt_inactive": not context["halt_active"],
    }
    eligible = all(safeguards.values())
    return {
        "posture": "controlled_offense" if eligible else "normal",
        "controlled_offense_eligible": eligible,
        "safeguards": safeguards,
        "target_allocation_if_candidates_qualify": ({
            "qqqm_weight": rules["target_qqqm_weight"],
            "vti_weight": rules["target_vti_weight"],
            "individual_stocks_total_max": rules["target_total_stock_weight"],
            "single_stock_max": rules["target_single_stock_weight"],
            "stock_positions_max": rules["target_max_stock_positions"],
            "sgov_or_cash_min": rules["minimum_defensive_weight"],
        } if eligible else None),
        "trade_authorized": False,
        "note": ("受控进攻只把组合从ETF/防御资产向通过全部硬门的个股倾斜，"
                 "风险仍不得超过既有15%单股、30%个股合计、90%总股票、"
                 "10%防御底仓及回撤限制。没有合格候选就保持原配置。"),
    }


def evaluate(payload, policy=None):
    policy = policy or load_policy()
    periods = payload.get("periods")
    if not isinstance(periods, list) or not periods:
        raise ValueError("periods_nonempty_list_required")
    objective = policy["performance_objective"]
    challenge_result = evaluate_challenge(payload.get("challenge"), policy)
    daily_result = evaluate_daily(payload.get("daily"))
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
    posture = evaluate_risk_posture(
        challenge_result, rows, payload.get("risk_context"), policy)
    return {
        "objective": "outperform_both_voo_and_qqq",
        "daily_objective": daily_result,
        "periods": rows,
        "profit_challenge": challenge_result,
        "risk_posture": posture,
        "expand_qualified_stock_search": expand,
        "trade_authorized": False,
        "note": ("落后先提高合格个股研究；只有全部受控进攻条件满足时，才在既有硬上限内"
                 "把配置倾向合格个股。任何实际订单仍须通过全部硬门，且不保证收益。"),
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
