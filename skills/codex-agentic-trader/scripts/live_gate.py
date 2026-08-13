#!/usr/bin/env python3
"""实盘两阶段确定性闸门（codex-agentic-trader）。

在 roy-trading-desk 的 decision_gate 全量检查之上扩展：
- mandate 授权推导模式（live/shadow），会话无权声明模式；
- HALT 哨兵文件无条件停止下单；
- 本地 intent 与券商订单对账，不平拒新单；
- 两阶段：pre_preview（最多放行 can_preview）→ post_preview（校验 preview 回执后才 can_submit）；
- 回撤触及最深档输出 halt_required=true；
- mandate 上限与 policy 取更严。

本脚本只输出布尔判定与批准的 intent，自身没有任何券商调用能力。
一切异常 fail-closed。
"""
import argparse
import hashlib
import json
import math
import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
POLICY_PATH = SCRIPT_DIR.parent / "policy" / "policy.json"

# 复用 roy-trading-desk 的基础检查实现，保持单一实现来源
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


def _keychain_checksum(service):
    """读取 Keychain 中存储的 mandate sha256；不可用/不存在返回 None。"""
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


def check_halt(policy):
    return _expand(policy["halt_file"]).exists()


def load_mandate(policy, today=None, skip_keychain=False):
    """返回 (mandate dict | None, reason)。任何不满足 → (None, 原因)，调用方降级 shadow。"""
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
    today = today or date.today().isoformat()
    exp = m.get("expires_at_et")
    if not (isinstance(exp, str) and len(exp) == 10 and exp >= today):
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
    broker_by_key = {r["client_key"]: r for r in broker}
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


def check_preview(preview, policy, violations):
    if not isinstance(preview, dict):
        violations.append("missing_preview")
        return
    if preview.get("preflight_status") != "clean":
        violations.append("preview_not_clean")
    if preview.get("warnings") != []:
        violations.append("preview_has_warnings")
    if not (isinstance(preview.get("preview_id"), str) and preview["preview_id"]):
        violations.append("preview_id_missing")
    age = preview.get("preview_age_seconds")
    if not (_is_finite_number(age) and 0 <= age <= policy["max_preview_age_seconds"]):
        violations.append("preview_stale")
    qp = preview.get("quoted_price")
    if not (_is_finite_number(qp) and qp > 0):
        violations.append("preview_quoted_price_invalid")


def evaluate(payload, policy, today=None, skip_keychain=False):
    import decision_gate  # 基础检查的单一实现来源（roy-trading-desk）

    phase = payload.get("phase")
    if phase not in ("pre_preview", "post_preview"):
        return {"mode": "shadow", "phase": str(phase), "would_allow": False,
                "can_preview": False, "can_submit": False, "halt_required": False,
                "violations": ["invalid_phase"]}
    if "mode" in payload:
        return {"mode": "shadow", "phase": phase, "would_allow": False,
                "can_preview": False, "can_submit": False, "halt_required": False,
                "violations": ["mode_must_not_be_declared"]}

    # 模式推导：mandate + HALT
    halted = check_halt(policy)
    mandate, mandate_reason = load_mandate(policy, today=today, skip_keychain=skip_keychain)
    mode = "live" if (mandate and not halted) else "shadow"

    # 基础检查复用 decision_gate（其契约要求 mode=decide）
    base_payload = dict(payload)
    base_payload["mode"] = "decide"
    base_payload.pop("phase", None)
    base_payload.pop("reconciliation", None)
    base_payload.pop("preview", None)
    base = decision_gate.evaluate(base_payload, policy)
    violations = list(base["violations"])

    # mandate 上限收紧（live 才有 mandate；shadow 也照 policy 检查）
    prop = payload.get("proposal") or {}
    amount = prop.get("amount")
    is_risk_exit = (prop.get("side") == "sell"
                    and prop.get("trigger") in policy["risk_exit_triggers"])
    if mandate and _is_finite_number(amount) and not is_risk_exit:
        # 风险退出卖单豁免 mandate 额度：降风险动作不受授权额度阻拦（仍记账、仍走两阶段）
        if amount > mandate["max_order_amount"] + 1e-9:
            violations.append("mandate_order_amount_exceeded")
        hist = payload.get("history") or {}
        turnover = hist.get("turnover_today")
        if _is_finite_number(turnover) and turnover + amount > mandate["max_daily_turnover"] + 1e-9:
            violations.append("mandate_daily_turnover_exceeded")

    # 对账（两个 phase 都要）
    check_reconciliation(payload.get("reconciliation"), violations)

    # post_preview 校验
    if phase == "post_preview":
        check_preview(payload.get("preview"), policy, violations)

    # 回撤断路器
    halt_required = False
    derived = dict(base.get("derived") or {})
    dd = derived.get("drawdown")
    if isinstance(dd, (int, float)) and dd >= policy["halt_drawdown_threshold"]:
        halt_required = True

    if halted:
        violations.append("halt_active")

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
            "order_type": prop.get("order_type"),
            "client_key_required": True,
        }
    return result


def check_runtime(policy):
    """--check-runtime：安装态自检（mandate/HALT/policy 完整性），供每次运行开头调用。"""
    halted = check_halt(policy)
    mandate, reason = load_mandate(policy)
    report = {
        "halt_active": halted,
        "mandate_status": reason,
        "mode_if_run_now": "live" if (mandate and not halted) else "shadow",
        "policy_version": policy.get("policy_version"),
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
    try:
        result = evaluate(payload, policy)
    except Exception as exc:  # noqa: BLE001
        _fail([f"gate_internal_error:{type(exc).__name__}"], phase=str(payload.get("phase")))
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["would_allow"] else 1)


if __name__ == "__main__":
    main()
