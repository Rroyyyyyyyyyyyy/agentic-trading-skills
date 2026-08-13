#!/usr/bin/env python3
"""实盘两阶段确定性闸门（codex-agentic-trader）。

在 decision_gate 全量基础检查之上扩展：
- mandate 授权推导模式（live/shadow），会话无权声明模式；到期日真实日历解析；
- HALT 哨兵：存在时只放行风险退出卖单，其余全拒；halt_required 时闸门自写 HALT（自锁）；
- 本地 intent 与券商订单对账（含重复 client_key 冲突检测），不平拒新单；
- 两阶段：pre_preview → post_preview；preview 回执必须与提案逐字段绑定且一次性消费；
- 闸门本地状态账本（gate_state_file）：当日已批订单数/金额/decision_key/preview_id 由闸门
  自己记账，与调用方声明取更严——频次/换手/幂等不再单信调用方（对抗审查 P2 修复）；
- limit_day 单必须携带限价；approved_intent 与 preview_id/decision_key/报价绑定锁单。

本脚本只输出判定与批准的 intent，自身没有任何券商调用能力。一切异常 fail-closed。
"""
import argparse
import fcntl
import hashlib
import json
import math
import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
POLICY_PATH = SCRIPT_DIR.parent / "policy" / "policy.json"
sys.path.insert(0, str(SCRIPT_DIR))

VALID_ORDER_STATUSES = {"open", "partially_filled", "filled", "cancelled", "rejected", "unknown"}
TERMINAL_STATUSES = {"filled", "cancelled", "rejected"}
MANDATE_CONFIRMATION_TEMPLATE = "I AUTHORIZE LIVE TRADING {last4}"


def _fail(violations, phase="unknown"):
    print(json.dumps({
        "mode": "shadow", "phase": phase, "would_allow": False,
        "can_preview": False, "can_submit": False, "halt_required": False,
        "violations": violations,
    }, ensure_ascii=False))
    sys.exit(1)


def _is_finite_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def load_policy(path=None):
    with open(path or POLICY_PATH) as fh:
        return json.load(fh)


def _expand(p):
    return Path(p).expanduser()


def _parse_iso_date(s):
    """严格 ISO 日期解析；任何非规范形式返回 None（P3 修复：不再用字符串比较）。"""
    if not isinstance(s, str) or len(s) != 10:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def _keychain_checksum(service):
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def check_halt(policy):
    return _expand(policy["halt_file"]).exists()


def write_halt(policy):
    p = _expand(policy["halt_file"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()


def load_mandate(policy, today=None, skip_keychain=False):
    path = _expand(policy["mandate_file"])
    if not path.exists():
        return None, "mandate_missing"
    try:
        mode = path.stat().st_mode & 0o777
        if mode & 0o077:
            return None, "mandate_permissions_too_open"
        raw = path.read_text()
        m = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None, "mandate_unreadable"
    if m.get("mandate_version") != "1.0":
        return None, "mandate_version_unsupported"
    if m.get("account_last4") != policy["account_last4"]:
        return None, "mandate_account_mismatch"
    expected = MANDATE_CONFIRMATION_TEMPLATE.format(last4=policy["account_last4"])
    if m.get("confirmation") != expected:
        return None, "mandate_confirmation_invalid"
    today_d = _parse_iso_date(today or date.today().isoformat())
    exp_d = _parse_iso_date(m.get("expires_at_et"))
    if today_d is None or exp_d is None or exp_d < today_d:
        return None, "mandate_expired"
    for f in ("max_order_amount", "max_daily_turnover"):
        if not _is_finite_number(m.get(f)) or m[f] <= 0:
            return None, f"mandate_invalid_{f}"
    if not skip_keychain:
        stored = _keychain_checksum(policy["keychain_service"])
        actual = hashlib.sha256(raw.encode()).hexdigest()
        if stored is None or stored != actual:
            return None, "mandate_checksum_mismatch"
    return m, "ok"


# ---------- 闸门本地状态账本（P2 修复） ----------

def empty_state(date_et):
    return {"date_et": date_et, "orders": []}


def load_state(path, date_et):
    """读取状态账本；换日或不可读则重置为空（保守方向：空账本只会让检查更依赖调用方，
    因此不可读时保留 fail-closed 标记由调用处理）。"""
    p = _expand(path)
    if not p.exists():
        return empty_state(date_et), "ok"
    try:
        s = json.loads(p.read_text())
        if not (isinstance(s, dict) and isinstance(s.get("orders"), list)):
            return None, "state_corrupt"
        if s.get("date_et") != date_et:
            return empty_state(date_et), "ok"
        for o in s["orders"]:
            if not (isinstance(o, dict) and isinstance(o.get("decision_key"), str)
                    and _is_finite_number(o.get("amount"))):
                return None, "state_corrupt"
        return s, "ok"
    except Exception:  # noqa: BLE001
        return None, "state_corrupt"


def save_state(path, state):
    p = _expand(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(state, ensure_ascii=False))
        fh.flush()
        import os
        os.fsync(fh.fileno())


def record_submission(state, decision_key, preview_id, amount):
    state["orders"].append({"decision_key": decision_key,
                            "preview_id": preview_id, "amount": amount})
    return state


# ---------- 检查块 ----------

def check_reconciliation(recon, violations):
    if not isinstance(recon, dict):
        violations.append("missing_reconciliation")
        return
    intents = recon.get("local_intents")
    broker = recon.get("broker_orders")
    if not isinstance(intents, list) or not isinstance(broker, list):
        violations.append("invalid_reconciliation")
        return
    def _valid(rows):
        for r in rows:
            if not (isinstance(r, dict) and isinstance(r.get("client_key"), str)
                    and r.get("client_key") and r.get("status") in VALID_ORDER_STATUSES):
                return False
        return True
    if not (_valid(intents) and _valid(broker)):
        violations.append("invalid_reconciliation_entry")
        return
    broker_by_key = {}
    for r in broker:
        k = r["client_key"]
        if k in broker_by_key and broker_by_key[k]["status"] != r["status"]:
            # P7 修复：同 client_key 冲突状态 = 券商侧不一致
            violations.append("reconciliation_mismatch")
            return
        broker_by_key[k] = r
    local_keys = {r["client_key"] for r in intents}
    for it in intents:
        if it["status"] == "unknown":
            violations.append("reconciliation_mismatch")
            return
        if it["status"] not in TERMINAL_STATUSES and it["client_key"] not in broker_by_key:
            violations.append("reconciliation_mismatch")
            return
    for br in broker:
        if br["status"] == "unknown" or br["client_key"] not in local_keys:
            violations.append("reconciliation_mismatch")
            return


def check_preview(preview, policy, prop, used_preview_ids, violations):
    """P1 修复：回执必须与提案逐字段绑定，且 preview_id 一次性消费。"""
    if not isinstance(preview, dict):
        violations.append("missing_preview")
        return
    if preview.get("preflight_status") != "clean":
        violations.append("preview_not_clean")
    if preview.get("warnings") != []:
        violations.append("preview_has_warnings")
    pid = preview.get("preview_id")
    if not (isinstance(pid, str) and pid):
        violations.append("preview_id_missing")
    elif pid in used_preview_ids:
        violations.append("preview_replayed")
    age = preview.get("preview_age_seconds")
    if not (_is_finite_number(age) and 0 <= age <= policy["max_preview_age_seconds"]):
        violations.append("preview_stale")
    qp = preview.get("quoted_price")
    if not (_is_finite_number(qp) and qp > 0):
        violations.append("preview_quoted_price_invalid")
    # 与提案绑定：symbol/side/amount 必须逐字段一致
    if preview.get("symbol") != prop.get("symbol") or preview.get("side") != prop.get("side"):
        violations.append("preview_proposal_mismatch")
    amt = preview.get("amount")
    if not (_is_finite_number(amt) and _is_finite_number(prop.get("amount"))
            and abs(amt - prop["amount"]) <= 0.01):
        violations.append("preview_proposal_mismatch")


def evaluate(payload, policy, today=None, skip_keychain=False, state=None):
    import decision_gate

    phase = payload.get("phase")
    if phase not in ("pre_preview", "post_preview"):
        return {"mode": "shadow", "phase": str(phase), "would_allow": False,
                "can_preview": False, "can_submit": False, "halt_required": False,
                "violations": ["invalid_phase"]}
    if "mode" in payload:
        return {"mode": "shadow", "phase": phase, "would_allow": False,
                "can_preview": False, "can_submit": False, "halt_required": False,
                "violations": ["mode_must_not_be_declared"]}

    halted = check_halt(policy)
    mandate, mandate_reason = load_mandate(policy, today=today, skip_keychain=skip_keychain)
    mode = "live" if mandate else "shadow"

    prop = payload.get("proposal") if isinstance(payload.get("proposal"), dict) else {}
    is_risk_exit = (prop.get("side") == "sell"
                    and prop.get("trigger") in policy["risk_exit_triggers"])

    # 状态账本合并：频次/换手/幂等取「调用方声明 ∪ 闸门自身记录」的更严值（P2 修复）
    violations = []
    state_orders = (state or {}).get("orders", []) if state is not None else []
    hist = payload.get("history") if isinstance(payload.get("history"), dict) else {}
    merged_hist = dict(hist)
    caller_orders = hist.get("orders_today")
    caller_turnover = hist.get("turnover_today")
    if _is_finite_number(caller_orders):
        merged_hist["orders_today"] = max(caller_orders, len(state_orders))
    if _is_finite_number(caller_turnover):
        merged_hist["turnover_today"] = max(
            caller_turnover, sum(o["amount"] for o in state_orders))
    state_keys = [o["decision_key"] for o in state_orders]
    caller_keys = hist.get("used_decision_keys")
    merged_hist["used_decision_keys"] = (
        (caller_keys if isinstance(caller_keys, list) else []) + state_keys)
    used_preview_ids = set(
        o.get("preview_id") for o in state_orders if o.get("preview_id"))
    caller_pids = hist.get("used_preview_ids")
    if isinstance(caller_pids, list):
        used_preview_ids.update(p for p in caller_pids if isinstance(p, str))

    # 基础检查复用 decision_gate
    base_payload = dict(payload)
    base_payload["mode"] = "decide"
    base_payload["history"] = merged_hist
    base_payload.pop("phase", None)
    recon_sub = base_payload.pop("reconciliation", None)
    preview_sub = base_payload.pop("preview", None)
    base = decision_gate.evaluate(base_payload, policy)
    violations += list(base["violations"])

    # P6 修复：被 pop 的子树也要过账号泄漏扫描
    decision_gate._scan_account_like({"reconciliation": recon_sub, "preview": preview_sub},
                                     violations)

    # limit_day 必须带限价，market_day 不得带（P4 修复）
    limit_price = prop.get("limit_price")
    if prop.get("order_type") == "limit_day":
        if not (_is_finite_number(limit_price) and limit_price > 0):
            violations.append("limit_price_required")
    elif limit_price is not None:
        violations.append("limit_price_forbidden_for_market_order")

    # mandate 额度（风险退出卖单豁免）
    amount = prop.get("amount")
    if mandate and _is_finite_number(amount) and not is_risk_exit:
        if amount > mandate["max_order_amount"] + 1e-9:
            violations.append("mandate_order_amount_exceeded")
        turnover = merged_hist.get("turnover_today")
        if _is_finite_number(turnover) and turnover + amount > mandate["max_daily_turnover"] + 1e-9:
            violations.append("mandate_daily_turnover_exceeded")

    # 对账
    check_reconciliation(recon_sub, violations)

    # HALT：存在时只放行风险退出卖单（hard-boundaries §4 清仓通道）
    if halted and not is_risk_exit:
        violations.append("halt_active")

    # post_preview 校验（含提案绑定与重放检测）
    if phase == "post_preview":
        check_preview(preview_sub, policy, prop, used_preview_ids, violations)

    # 回撤断路器
    halt_required = False
    derived = dict(base.get("derived") or {})
    dd = derived.get("drawdown")
    if isinstance(dd, (int, float)) and dd >= policy["halt_drawdown_threshold"]:
        halt_required = True

    violations = list(dict.fromkeys(violations))
    would_allow = not violations
    result = {
        "mode": mode,
        "phase": phase,
        "would_allow": would_allow,
        "halt_required": halt_required,
        "decision_key": base.get("decision_key"),
        "violations": violations,
        "derived": derived,
        "mandate_status": mandate_reason,
        "can_preview": False,
        "can_submit": False,
    }
    if mandate:
        result["derived"]["mandate_expires_at_et"] = mandate["expires_at_et"]
    if mode == "shadow":
        result["shadow_would_allow"] = would_allow
        return result
    if would_allow and phase == "pre_preview":
        result["can_preview"] = True
    if would_allow and phase == "post_preview":
        result["can_submit"] = True
        result["approved_intent"] = {
            "symbol": prop.get("symbol"),
            "side": prop.get("side"),
            "amount": amount,
            "asset_class": prop.get("asset_class"),
            "order_type": prop.get("order_type"),
            "limit_price": limit_price,
            "trigger": prop.get("trigger"),
            "decision_key": base.get("decision_key"),
            "preview_id": (preview_sub or {}).get("preview_id"),
            "quoted_price": (preview_sub or {}).get("quoted_price"),
            "client_key_required": True,
        }
    return result


def check_runtime(policy):
    halted = check_halt(policy)
    mandate, reason = load_mandate(policy)
    report = {
        "halt_active": halted,
        "mandate_status": reason,
        "mode_if_run_now": "live" if mandate else "shadow",
        "policy_version": policy.get("policy_version"),
        "note": "HALT 存在时仅允许风险退出卖单" if halted else None,
    }
    if mandate:
        report["mandate_expires_at_et"] = mandate["expires_at_et"]
    print(json.dumps(report, ensure_ascii=False))
    sys.exit(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="决策 JSON 文件路径；缺省读 stdin")
    ap.add_argument("--policy", help="policy.json 路径（默认 skill 内置）")
    ap.add_argument("--check-runtime", action="store_true")
    args = ap.parse_args()

    def _reject_dup_keys(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate_json_keys")
        return dict(pairs)

    try:
        policy = load_policy(args.policy)
    except Exception as exc:  # noqa: BLE001
        _fail([f"policy_error:{type(exc).__name__}"])
    if args.check_runtime:
        check_runtime(policy)
    try:
        raw = Path(args.input).read_text() if args.input else sys.stdin.read()
        payload = json.loads(raw, object_pairs_hook=_reject_dup_keys)
    except Exception as exc:  # noqa: BLE001
        _fail([f"input_error:{type(exc).__name__}"])

    date_et = str(payload.get("as_of_et", ""))[:10]
    state, state_status = load_state(policy["gate_state_file"], date_et)
    if state_status != "ok":
        _fail(["gate_state_corrupt"], phase=str(payload.get("phase")))
    try:
        result = evaluate(payload, policy, state=state)
    except Exception as exc:  # noqa: BLE001
        _fail([f"gate_internal_error:{type(exc).__name__}"], phase=str(payload.get("phase")))

    # P5 修复：断路器自锁——halt_required 时闸门自己写 HALT（幂等），不依赖 runner 履约
    if result.get("halt_required"):
        try:
            write_halt(policy)
        except Exception:  # noqa: BLE001
            result["violations"].append("halt_write_failed")
            result["would_allow"] = False
            result["can_preview"] = False
            result["can_submit"] = False
    # 批准提交即入本地账本（幂等/频次的闸门侧真相）
    if result.get("can_submit"):
        try:
            record_submission(state, result["decision_key"],
                              result["approved_intent"].get("preview_id"),
                              result["approved_intent"].get("amount"))
            save_state(policy["gate_state_file"], state)
        except Exception:  # noqa: BLE001
            result["can_submit"] = False
            result["would_allow"] = False
            result["violations"].append("gate_state_write_failed")

    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["would_allow"] else 1)


if __name__ == "__main__":
    main()
