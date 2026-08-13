#!/usr/bin/env python3
"""Create, inspect, and revoke the owner's bounded trading delegation.

A mandate is a governance envelope, not a security boundary and not a substitute for
Robinhood/Codex confirmation requirements.  TTY entry makes issuance deliberate, but an
agent with same-user shell access can allocate a PTY; callers must not describe this as
"agent unreachable".  The runtime therefore also requires exact tool scope, a pinned
account adapter, a matching policy/toolset fingerprint, and per-order confirmation in the
current supervised release.
"""
import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SKILL_DIR = Path(__file__).resolve().parent.parent
POLICY_PATH = SKILL_DIR / "policy" / "policy.json"
TOOLSET_PATH = SKILL_DIR / "policy" / "enabled_tools_live.json"


def _canonical_hash(obj):
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def load_policy():
    with open(POLICY_PATH) as fh:
        return json.load(fh)


def _load_toolset():
    with open(TOOLSET_PATH) as fh:
        return json.load(fh)


def _expand(p):
    return Path(p).expanduser()


def _now_et(policy):
    return datetime.now(ZoneInfo(policy["market_timezone"]))


def _keychain_set(service, value):
    subprocess.run(["security", "delete-generic-password", "-s", service],
                   capture_output=True)
    r = subprocess.run(
        ["security", "add-generic-password", "-s", service, "-a", "mandate-sha256",
         "-w", value, "-U"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("keychain_write_failed")


def _keychain_delete(service):
    subprocess.run(["security", "delete-generic-password", "-s", service],
                   capture_output=True)


def _finite_positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value) and value > 0


def _validate_create_args(args, policy):
    if not 1 <= args.days <= policy["max_mandate_days"]:
        raise ValueError("days_out_of_range")
    if args.execution_mode not in policy["supported_execution_modes"]:
        raise ValueError("execution_mode_unsupported")
    if not _finite_positive(args.max_order) or args.max_order > policy["capital_cap"]:
        raise ValueError("max_order_out_of_range")
    policy_turnover = policy["max_daily_turnover_ratio"] * policy["capital_cap"]
    if not _finite_positive(args.max_daily_turnover) or args.max_daily_turnover > policy_turnover:
        raise ValueError("max_daily_turnover_out_of_range")
    if not 1 <= args.max_orders <= policy["max_orders_per_day"]:
        raise ValueError("max_orders_out_of_range")


def cmd_create(args, policy):
    try:
        _validate_create_args(args, policy)
    except ValueError as exc:
        print(f"拒绝：{exc}。")
        sys.exit(2)
    if not sys.stdin.isatty():
        print("拒绝：create 只接受本地交互终端输入。TTY 是签发仪式，不是密码学隔离。")
        sys.exit(2)
    last4 = policy["account_last4"]
    expected = f"I AUTHORIZE SUPERVISED ROBINHOOD TRADING {last4}"
    print(f"即将签发受监督委托：账户 ••••{last4}，有效期 {args.days} 天，"
          f"单笔上限 ${args.max_order:.2f}，每日上限 {args.max_orders} 笔 / "
          f"${args.max_daily_turnover:.2f}。")
    print("本发布版仍要求逐笔展示 Robinhood review 结果并获得明确确认；"
          "mandate 不会绕过平台确认。")
    print(f"确认请完整输入：{expected}")
    typed = input("> ").strip()
    if typed != expected:
        print("确认语不匹配，未签发。")
        sys.exit(2)

    issued = _now_et(policy).replace(microsecond=0)
    mandate = {
        "mandate_version": "2.0",
        "mandate_id": str(uuid.uuid4()),
        "account_ref_masked": f"****{last4}",
        "execution_mode": args.execution_mode,
        "requires_per_order_confirmation": True,
        "issued_at_et": issued.isoformat(),
        "not_before_et": issued.isoformat(),
        "expires_at_et": (issued + timedelta(days=args.days)).isoformat(),
        "max_order_amount": float(args.max_order),
        "max_daily_turnover": float(args.max_daily_turnover),
        "max_orders_per_day": int(args.max_orders),
        "policy_sha256": _canonical_hash(policy),
        "toolset_sha256": _canonical_hash(_load_toolset()),
        "confirmation": expected,
    }
    path = _expand(policy["mandate_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(mandate, ensure_ascii=False, indent=2, allow_nan=False)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(path, 0o600)
    _keychain_set(policy["keychain_service"], hashlib.sha256(raw.encode()).hexdigest())
    print(f"已签发 mandate {mandate['mandate_id']}，至 {mandate['expires_at_et']} 有效。")


def cmd_revoke(_args, policy):
    path = _expand(policy["mandate_file"])
    if path.exists():
        path.unlink()
    _keychain_delete(policy["keychain_service"])
    print("mandate 已撤销；运行时立即降级为 shadow。")


def cmd_status(_args, policy):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import live_gate
    mandate, reason = live_gate.load_mandate(policy)
    halted = live_gate.check_halt(policy)
    out = {
        "mandate_status": reason,
        "halt_active": halted,
        "mode_if_run_now": "supervised" if (mandate and not halted) else "shadow",
    }
    if mandate:
        out.update({
            "mandate_id": mandate["mandate_id"],
            "account_ref_masked": mandate["account_ref_masked"],
            "execution_mode": mandate["execution_mode"],
            "expires_at_et": mandate["expires_at_et"],
            "max_order_amount": mandate["max_order_amount"],
            "max_daily_turnover": mandate["max_daily_turnover"],
            "max_orders_per_day": mandate["max_orders_per_day"],
            "requires_per_order_confirmation": True,
        })
    print(json.dumps(out, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    create = sub.add_parser("create")
    create.add_argument("--days", type=int, default=7)
    create.add_argument("--max-order", type=float, required=True)
    create.add_argument("--max-daily-turnover", type=float, required=True)
    create.add_argument("--max-orders", type=int, default=3)
    create.add_argument("--execution-mode", default="supervised_review")
    sub.add_parser("revoke")
    sub.add_parser("status")
    args = ap.parse_args()
    policy = load_policy()
    {"create": cmd_create, "revoke": cmd_revoke, "status": cmd_status}[args.cmd](args, policy)


if __name__ == "__main__":
    main()
