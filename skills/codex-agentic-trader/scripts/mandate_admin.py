#!/usr/bin/env python3
"""mandate 管理器——只供 Roy 本人在终端交互运行。

设计目标（借鉴 Vibe-Trading 的"授权动作 agent 不可达"思想）：
- create 需要 TTY 交互输入完整确认语，agent 的非交互调用拿不到 TTY 即失败；
- 文件 0600 + sha256 存入 macOS Keychain 供 live_gate 漂移检测；
- revoke/status 随时可用；revoke 立即使 live 降级 shadow。

诚实边界：同用户 shell 不是密码学隔离；本机制防的是 agent 例行自我授权与文件漂移，
不防有完整 shell 控制权的对抗者。
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

POLICY_PATH = Path(__file__).resolve().parent.parent / "policy" / "policy.json"


def load_policy():
    with open(POLICY_PATH) as fh:
        return json.load(fh)


def _expand(p):
    return Path(p).expanduser()


def _keychain_set(service, value):
    subprocess.run(["security", "delete-generic-password", "-s", service],
                   capture_output=True)
    r = subprocess.run(
        ["security", "add-generic-password", "-s", service, "-a", "mandate-sha256",
         "-w", value, "-U"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"keychain_write_failed:{r.stderr.strip()}")


def _keychain_delete(service):
    subprocess.run(["security", "delete-generic-password", "-s", service],
                   capture_output=True)


def cmd_create(args, policy):
    if not sys.stdin.isatty():
        print("拒绝：create 只能在交互终端由 Roy 本人运行（无 TTY）。")
        sys.exit(2)
    last4 = policy["account_last4"]
    expected = f"I AUTHORIZE LIVE TRADING {last4}"
    print(f"即将签发实盘 mandate：账户 ••••{last4}，有效期 {args.days} 天，"
          f"单笔上限 ${args.max_order:.2f}，日换手上限 ${args.max_daily_turnover:.2f}。")
    print(f"确认请完整输入：{expected}")
    typed = input("> ").strip()
    if typed != expected:
        print("确认语不匹配，未签发。")
        sys.exit(2)
    mandate = {
        "mandate_version": "1.0",
        "account_last4": last4,
        "issued_at_et": date.today().isoformat(),
        "expires_at_et": (date.today() + timedelta(days=args.days)).isoformat(),
        "max_order_amount": float(args.max_order),
        "max_daily_turnover": float(args.max_daily_turnover),
        "confirmation": expected,
    }
    path = _expand(policy["mandate_file"])
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(mandate, ensure_ascii=False, indent=2)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(raw)
    os.chmod(path, 0o600)
    _keychain_set(policy["keychain_service"], hashlib.sha256(raw.encode()).hexdigest())
    print(f"已签发，至 {mandate['expires_at_et']} 有效。撤销：mandate_admin.py revoke")


def cmd_revoke(_args, policy):
    path = _expand(policy["mandate_file"])
    if path.exists():
        path.unlink()
    _keychain_delete(policy["keychain_service"])
    print("mandate 已撤销；live 立即降级为 shadow。")


def cmd_status(_args, policy):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import live_gate
    m, reason = live_gate.load_mandate(policy)
    halted = live_gate.check_halt(policy)
    out = {"mandate_status": reason, "halt_active": halted,
           "mode_if_run_now": "live" if (m and not halted) else "shadow"}
    if m:
        out["expires_at_et"] = m["expires_at_et"]
        out["max_order_amount"] = m["max_order_amount"]
        out["max_daily_turnover"] = m["max_daily_turnover"]
    print(json.dumps(out, ensure_ascii=False))


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--days", type=int, default=30)
    c.add_argument("--max-order", type=float, required=True)
    c.add_argument("--max-daily-turnover", type=float, required=True)
    sub.add_parser("revoke")
    sub.add_parser("status")
    args = ap.parse_args()
    policy = load_policy()
    {"create": cmd_create, "revoke": cmd_revoke, "status": cmd_status}[args.cmd](args, policy)


if __name__ == "__main__":
    main()
