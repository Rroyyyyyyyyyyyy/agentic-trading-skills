#!/usr/bin/env python3
"""行为画像（借鉴 HKUDS Vibe-Trading 的 Shadow Account 思想，本地简化实现）。

读链式日志中的 fill 事件，检测四类行为偏差并给出 if-then 建议。只做统计呈现，
不自动改任何策略参数；样本不足时如实标注 insufficient_data，不硬给结论。
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

MIN_SELL_SAMPLES = 4   # 处置效应最少卖出样本
MIN_BUY_SAMPLES = 4    # 追涨检测最少买入样本
CHASE_RUNUP_PCT = 10.0  # 买前 5 日涨幅超过此值视为追涨样本


def load_fills(log_path):
    fills = []
    for line in Path(log_path).read_text().splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        ev = entry.get("event", {})
        if ev.get("type") == "fill":
            fills.append(ev)
    return fills


def analyze(fills, max_orders_per_day):
    findings = []

    # 1. 过度交易
    by_day = defaultdict(int)
    for f in fills:
        by_day[f.get("date_et", "unknown")] += 1
    if by_day:
        days_at_cap = sum(1 for c in by_day.values() if c >= max_orders_per_day)
        avg = sum(by_day.values()) / len(by_day)
        findings.append({
            "bias": "overtrading",
            "flag": days_at_cap >= max(2, len(by_day) // 3),
            "stats": {"trading_days": len(by_day), "avg_fills_per_day": round(avg, 2),
                      "days_at_order_cap": days_at_cap},
            "suggestion": "if 当日笔数已达上限的 2/3 then 当天只出观察卡不出执行卡",
        })

    # 2. 处置效应：盈利卖出持有期 vs 亏损卖出持有期
    win_days, loss_days = [], []
    for f in fills:
        if f.get("side") == "sell" and isinstance(f.get("pnl"), (int, float)) \
                and isinstance(f.get("holding_days"), (int, float)):
            (win_days if f["pnl"] > 0 else loss_days).append(f["holding_days"])
    if len(win_days) + len(loss_days) >= MIN_SELL_SAMPLES and win_days and loss_days:
        aw, al = sum(win_days) / len(win_days), sum(loss_days) / len(loss_days)
        findings.append({
            "bias": "disposition_effect",
            "flag": aw < al * 0.5,
            "stats": {"avg_hold_days_winners": round(aw, 1), "avg_hold_days_losers": round(al, 1),
                      "sell_samples": len(win_days) + len(loss_days)},
            "suggestion": "if 想卖盈利单 then 先核对失效条件是否真触发；if 亏损单破失效条件 then 按卡执行不拖延",
        })
    else:
        findings.append({"bias": "disposition_effect", "flag": False,
                         "stats": {"note": "insufficient_data"}, "suggestion": None})

    # 3. 追涨：买入时 run_up_5d_pct
    runups = [f.get("run_up_5d_pct") for f in fills
              if f.get("side") == "buy" and isinstance(f.get("run_up_5d_pct"), (int, float))]
    if len(runups) >= MIN_BUY_SAMPLES:
        chases = sum(1 for r in runups if r > CHASE_RUNUP_PCT)
        findings.append({
            "bias": "chasing",
            "flag": chases / len(runups) > 0.5,
            "stats": {"buy_samples": len(runups), "chase_ratio": round(chases / len(runups), 2)},
            "suggestion": "if 候选 5 日涨幅>10% then 只接受回踩企稳形态，不接受追高突破",
        })
    else:
        findings.append({"bias": "chasing", "flag": False,
                         "stats": {"note": "insufficient_data"}, "suggestion": None})

    # 4. 决策卡偏离率
    devs = [f for f in fills if f.get("deviation") not in (None, "", "none")]
    if fills:
        ratio = len(devs) / len(fills)
        findings.append({
            "bias": "card_deviation",
            "flag": ratio > 0.3,
            "stats": {"fills": len(fills), "deviations": len(devs), "ratio": round(ratio, 2)},
            "suggestion": "if 连续 3 次偏离决策卡 then 先复盘卡的可执行性（金额粒度/时点），再出新卡",
        })
    return findings


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True)
    ap.add_argument("--max-orders-per-day", type=int, default=6)
    args = ap.parse_args()
    try:
        fills = load_fills(args.log)
        findings = analyze(fills, args.max_orders_per_day)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}:{exc}"}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps({"ok": True, "fill_count": len(fills), "findings": findings},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
