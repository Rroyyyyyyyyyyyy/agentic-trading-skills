#!/usr/bin/env python3
"""确定性决策闸门（shadow/manual-execution only）。

读取决策 JSON（契约见 references/decision-card.md），按固定顺序执行全部检查，
输出 would_allow 与 manual_card。本脚本没有、也永远不得有任何券商写能力；
execution_capability 恒为 "none"。任何异常、缺字段、非有限数、过期数据一律拒绝
（fail-closed）。仓位上限、回撤档、频次全部内部推导，不信任调用方声明。
"""
import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parent.parent / "policy" / "policy.json"

# 疑似完整账号：8-20 位纯数字
_ACCOUNT_LIKE_MIN = 8
_ACCOUNT_LIKE_MAX = 20


def _fail(violations):
    print(json.dumps({
        "would_allow": False,
        "violations": violations,
        "execution_capability": "none",
    }, ensure_ascii=False))
    sys.exit(1)


def _is_finite_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _require(cond, code, violations):
    if not cond:
        violations.append(code)


def _is_hex_digest(s):
    return len(s) in (40, 64) and all(c in "0123456789abcdef" for c in s.lower())


def _strip_seps(s):
    for sep in (" ", "-", "_", "."):
        s = s.replace(sep, "")
    return s


def _has_digit_run(s, min_len):
    run = 0
    for ch in s + "\0":
        if ch.isdigit():
            run += 1
        else:
            if run >= min_len:
                return True
            run = 0
    return False


def _scan_account_like(obj, violations):
    """递归扫描字符串值，≥8 位连续数字（先剥离空格/连字符等分隔符）视为疑似完整账号。

    sha1/sha256 十六进制摘要（decision_key 等）豁免，避免对合法哈希误报。
    """
    if isinstance(obj, dict):
        for v in obj.values():
            _scan_account_like(v, violations)
    elif isinstance(obj, list):
        for v in obj:
            _scan_account_like(v, violations)
    elif isinstance(obj, str):
        if _is_hex_digest(obj):
            return
        if _has_digit_run(obj, _ACCOUNT_LIKE_MIN) or _has_digit_run(_strip_seps(obj), 10):
            # 规则 a：原始串 ≥8 位连续数字；规则 b：剥离分隔符后 ≥10 位
            # （b 的阈值取 10 是为放行 ISO 日期 2026-08-13 → 20260813 恰 8 位）
            violations.append("account_like_identifier_present")


def load_policy(path=None):
    with open(path or POLICY_PATH) as fh:
        return json.load(fh)


def compute_decision_key(date_et, symbol, side, trigger, amount):
    raw = f"{date_et}|{symbol}|{side}|{trigger}|{amount:.2f}"
    return hashlib.sha256(raw.encode()).hexdigest()


def evaluate(payload, policy):
    violations = []

    # 0. 顶层结构
    _require(payload.get("mode") == "decide", "mode_must_be_decide", violations)
    _require(payload.get("schema_version") == "1.0", "schema_version_unsupported", violations)
    if violations:
        return {"would_allow": False, "violations": violations, "execution_capability": "none"}

    _scan_account_like(payload, violations)
    if violations:
        return {"would_allow": False, "violations": violations, "execution_capability": "none"}

    # 1. 数据新鲜度
    age = payload.get("data_age_seconds")
    _require(_is_finite_number(age) and 0 <= age <= policy["max_data_age_seconds"],
             "stale_or_invalid_data_age", violations)
    as_of = payload.get("as_of_et")
    _require(isinstance(as_of, str) and len(as_of) >= 10, "missing_as_of_et", violations)

    # 2. 账户
    acct = payload.get("account")
    if not isinstance(acct, dict):
        violations.append("missing_account")
        return {"would_allow": False, "violations": violations, "execution_capability": "none"}
    equity = acct.get("equity")
    cash = acct.get("cash_available_settled")
    peak = acct.get("peak_equity_adjusted")
    _require(_is_finite_number(equity) and equity > 0, "invalid_equity", violations)
    _require(_is_finite_number(cash) and cash >= 0, "invalid_cash", violations)
    _require(_is_finite_number(peak) and peak > 0, "invalid_peak_equity", violations)
    positions = acct.get("positions")
    _require(isinstance(positions, list), "invalid_positions", violations)
    if violations:
        return {"would_allow": False, "violations": violations, "execution_capability": "none"}
    for p in positions:
        if not (isinstance(p, dict) and isinstance(p.get("symbol"), str)
                and _is_finite_number(p.get("value")) and p.get("value") >= 0
                and p.get("asset_class") in policy["allowed_asset_classes"]):
            violations.append("invalid_position_entry")
            return {"would_allow": False, "violations": violations, "execution_capability": "none"}

    # 3. 市场状态
    ms = payload.get("market_state")
    if not (isinstance(ms, dict)
            and isinstance(ms.get("benchmark_a_pass"), bool)
            and isinstance(ms.get("benchmark_b_pass"), bool)):
        violations.append("invalid_market_state")
        return {"would_allow": False, "violations": violations, "execution_capability": "none"}
    n_pass = int(ms["benchmark_a_pass"]) + int(ms["benchmark_b_pass"])
    caps = policy["market_state_stock_caps"]
    cap_market = caps["both_pass"] if n_pass == 2 else caps["one_pass"] if n_pass == 1 else caps["none_pass"]

    # 4. 回撤门
    drawdown = max(0.0, 1.0 - equity / peak)
    cap_drawdown = 1.0
    for tier in sorted(policy["drawdown_tiers"], key=lambda t: t["threshold"]):
        if drawdown >= tier["threshold"]:
            cap_drawdown = tier["stock_cap"]
    cap_effective = min(cap_market, cap_drawdown)

    # 5. 提案
    prop = payload.get("proposal")
    if not isinstance(prop, dict):
        violations.append("missing_proposal")
        return {"would_allow": False, "violations": violations, "execution_capability": "none"}
    symbol = prop.get("symbol")
    side = prop.get("side")
    amount = prop.get("amount")
    asset_class = prop.get("asset_class")
    trigger = prop.get("trigger")
    order_type = prop.get("order_type")
    _require(isinstance(symbol, str) and 1 <= len(symbol) <= 6 and symbol.isalpha(),
             "invalid_symbol", violations)
    _require(side in ("buy", "sell"), "invalid_side", violations)
    _require(_is_finite_number(amount) and amount >= policy["min_order_amount"],
             "amount_below_min", violations)
    _require(asset_class in policy["allowed_asset_classes"], "asset_class_forbidden", violations)
    # 资产类别符号白名单交叉校验（收紧"声明即信任"面：ETF/现金等价物只认 policy 白名单）
    if asset_class == "etf":
        _require(symbol in policy["etf_symbols"], "etf_symbol_not_whitelisted", violations)
    if asset_class == "cash_equiv":
        _require(symbol in policy["cash_equiv_symbols"], "cash_equiv_symbol_not_whitelisted", violations)
    _require(trigger in policy["valid_triggers"], "invalid_trigger", violations)
    _require(order_type in policy["allowed_order_types"], "invalid_order_type", violations)
    for field in ("thesis", "counter_thesis", "invalidation"):
        _require(isinstance(prop.get(field), str) and prop.get(field).strip() != "",
                 f"missing_{field}", violations)
    if violations:
        return {"would_allow": False, "violations": violations, "execution_capability": "none"}

    declares = prop.get("declares")
    if not isinstance(declares, dict):
        violations.append("invalid_declares")
        return {"would_allow": False, "violations": violations, "execution_capability": "none"}
    # 全类别：杠杆/反向与 OTC 声明必填且为 False
    _require(declares.get("is_leveraged_or_inverse") is False, "leveraged_or_inverse_forbidden", violations)
    _require(declares.get("is_otc") is False, "otc_forbidden", violations)

    # 6. 个股买入准入
    if side == "buy" and asset_class == "stock":
        entry = policy["stock_entry"]
        _require(_is_finite_number(declares.get("price")) and declares["price"] >= entry["min_price"],
                 "price_below_min", violations)
        _require(_is_finite_number(declares.get("market_cap"))
                 and declares["market_cap"] >= entry["min_market_cap"],
                 "market_cap_below_min", violations)
        _require(_is_finite_number(declares.get("avg_daily_volume"))
                 and declares["avg_daily_volume"] >= entry["min_avg_daily_volume"],
                 "volume_below_min", violations)
        for flag in ("above_50dma", "above_200dma", "ret_3m_positive", "ret_6m_positive",
                     "rel_strength_vs_benchmark_20d", "no_thesis_breaking_news"):
            _require(declares.get(flag) is True, f"entry_flag_failed_{flag}", violations)
        dte = declares.get("days_to_earnings")
        _require(_is_finite_number(dte) and dte >= entry["min_days_to_earnings"],
                 "too_close_to_earnings", violations)

    # 7. 仓位推导（买入后权重；卖出减少敞口不做上限检查）
    stock_value = sum(p["value"] for p in positions if p["asset_class"] == "stock")
    etf_value = sum(p["value"] for p in positions if p["asset_class"] == "etf")
    # 只统计与提案同 symbol 且同 asset_class 的持仓：防同名跨类"幽灵仓位"绕过
    # 新开仓计数（H1），也防跨类卖出（对不持有的类别下卖单）
    this_value = sum(p["value"] for p in positions
                     if p["symbol"] == symbol and p["asset_class"] == asset_class)
    stock_count = len({p["symbol"] for p in positions if p["asset_class"] == "stock"})
    derived = {
        "drawdown": round(drawdown, 4),
        "stock_cap_market_state": cap_market,
        "stock_cap_drawdown": cap_drawdown,
        "stock_cap_effective": cap_effective,
    }
    if side == "buy":
        equity_after = equity  # 现金换持仓，净值近似不变
        if asset_class == "stock":
            single_after = (this_value + amount) / equity_after
            stocks_after = (stock_value + amount) / equity_after
            equities_after = (stock_value + etf_value + amount) / equity_after
            _require(single_after <= policy["single_stock_max_weight"] + 1e-9,
                     "single_stock_over_cap", violations)
            _require(stocks_after <= policy["stocks_total_max_weight"] + 1e-9,
                     "stocks_total_over_cap", violations)
            is_new_position = this_value == 0
            _require(stock_count + (1 if is_new_position else 0) <= policy["max_stock_positions"],
                     "too_many_stock_positions", violations)
            derived["single_stock_weight_after"] = round(single_after, 4)
            derived["stocks_total_weight_after"] = round(stocks_after, 4)
        else:
            equities_after = (stock_value + etf_value + (amount if asset_class == "etf" else 0)) / equity_after
        if asset_class in ("stock", "etf"):
            _require(equities_after <= cap_effective + 1e-9, "equity_exposure_over_cap", violations)
            derived["equity_exposure_after"] = round(equities_after, 4)
        _require(amount <= cash + 1e-9, "insufficient_settled_cash", violations)
    else:  # sell：必须实际持有且金额不超过持仓市值（禁止裸卖空）
        _require(this_value > 0, "sell_position_not_held", violations)
        _require(amount <= this_value + 1e-9, "sell_exceeds_position", violations)

    # 8. 频次与换手（风险退出卖单豁免，但仍记账）
    hist = payload.get("history") or {}
    orders_today = hist.get("orders_today")
    turnover_today = hist.get("turnover_today")
    _require(_is_finite_number(orders_today) and orders_today >= 0, "invalid_orders_today", violations)
    _require(_is_finite_number(turnover_today) and turnover_today >= 0, "invalid_turnover_today", violations)
    is_risk_exit = side == "sell" and trigger in policy["risk_exit_triggers"]
    if not is_risk_exit and _is_finite_number(orders_today) and _is_finite_number(turnover_today):
        _require(orders_today < policy["max_orders_per_day"], "daily_order_limit_reached", violations)
        _require(turnover_today + amount <= policy["max_daily_turnover_ratio"] * equity + 1e-9,
                 "daily_turnover_over_cap", violations)

    # 9. 幂等
    date_et = as_of[:10] if isinstance(as_of, str) else ""
    key = compute_decision_key(date_et, symbol or "", side or "", trigger or "",
                               amount if _is_finite_number(amount) else 0.0)
    used = hist.get("used_decision_keys") or []
    _require(key not in used, "duplicate_decision", violations)

    would_allow = not violations
    result = {
        "would_allow": would_allow,
        "decision_key": key,
        "violations": violations,
        "derived": derived,
        "execution_capability": "none",
    }
    if would_allow:
        result["manual_card"] = {
            "action": f"{side.upper()} {symbol} ${amount:.2f}",
            "order_type": order_type,
            "trigger": trigger,
            "thesis": prop["thesis"],
            "counter_thesis": prop["counter_thesis"],
            "invalidation": prop["invalidation"],
            "as_of_et": as_of,
            "note": "由 Roy 本人在券商 App 手动执行；执行或放弃后回报记账。本卡为规则化研究产物，非投资建议。",
        }
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="决策 JSON 文件路径；缺省读 stdin")
    ap.add_argument("--policy", help="policy.json 路径（默认 skill 内置）")
    args = ap.parse_args()
    def _reject_dup_keys(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_json_keys")
        return dict(pairs)

    try:
        raw = Path(args.input).read_text() if args.input else sys.stdin.read()
        payload = json.loads(raw, object_pairs_hook=_reject_dup_keys)
        policy = load_policy(args.policy)
    except Exception as exc:  # noqa: BLE001
        _fail([f"input_error:{type(exc).__name__}"])
    try:
        result = evaluate(payload, policy)
    except Exception as exc:  # noqa: BLE001 — 任何未预期异常都以契约 JSON 形式 fail-closed
        _fail([f"gate_internal_error:{type(exc).__name__}"])
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["would_allow"] else 1)


if __name__ == "__main__":
    main()
