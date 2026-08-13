#!/usr/bin/env python3
"""Codex 侧 Robinhood MCP 工具面闸门（supervised_review）。

用法：codex mcp get robinhood-trading --json | python3 runtime_scope_gate_live.py

校验 server 名、官方 URL、enabled 和 enabled_tools 精确一致。它只证明
当前 Codex 配置的工具面，不证明账户绑定、不代替 Robinhood 平台确认，
更不是防止同用户 shell 绕过的安全边界。
"""
import json
import sys
from pathlib import Path

EXPECTED_PATH = Path(__file__).resolve().parent.parent / "policy" / "enabled_tools_live.json"
EXPECTED_SERVER = "robinhood-trading"
EXPECTED_URL = "https://agent.robinhood.com/mcp/trading"


def evaluate_scope(cfg, expected):
    transport = cfg.get("transport")
    if (cfg.get("name") != EXPECTED_SERVER or cfg.get("enabled") is not True
            or not isinstance(transport, dict)
            or transport.get("type") != "streamable_http"
            or transport.get("url") != EXPECTED_URL):
        return {"scope_pass": False, "reason": "server_identity_or_transport_mismatch"}
    tools = cfg.get("enabled_tools")
    if not isinstance(tools, list) or len(tools) != len(set(tools)):
        return {"scope_pass": False, "reason": "enabled_tools_missing_or_duplicate"}
    actual = set(tools)
    extra = sorted(actual - expected)
    missing = sorted(expected - actual)
    if extra or missing:
        return {"scope_pass": False, "extra": extra, "missing": missing}
    return {
        "scope_pass": True,
        "runtime_mode": "supervised_review",
        "tool_count": len(actual),
        "account_binding_verified": False,
        "autonomous_execution_authorized": False,
        "note": "tool surface only; exact account binding and broker confirmation are separate prerequisites",
    }


def main():
    try:
        cfg = json.loads(sys.stdin.read())
        expected = set(json.loads(EXPECTED_PATH.read_text())["expected_tools"])
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"scope_pass": False, "reason": f"input_error:{type(exc).__name__}"}))
        sys.exit(1)
    result = evaluate_scope(cfg, expected)
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["scope_pass"] else 1)


if __name__ == "__main__":
    main()
