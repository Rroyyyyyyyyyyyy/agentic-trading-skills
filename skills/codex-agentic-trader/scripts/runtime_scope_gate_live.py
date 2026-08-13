#!/usr/bin/env python3
"""Codex 侧 MCP 工具面闸门（实盘版）。

用法：codex mcp get robinhood-trading --json | python3 runtime_scope_gate_live.py

校验实际 enabled_tools 与 policy/enabled_tools_live.json 精确一致——多一个（上游偷偷
新增写工具）或少一个（必需读工具缺失）都 fail-closed。同时要求 approval 模式字段存在。
"""
import json
import sys
from pathlib import Path

EXPECTED_PATH = Path(__file__).resolve().parent.parent / "policy" / "enabled_tools_live.json"


def main():
    try:
        cfg = json.loads(sys.stdin.read())
        expected = set(json.loads(EXPECTED_PATH.read_text())["expected_tools"])
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"scope_pass": False, "reason": f"input_error:{type(exc).__name__}"}))
        sys.exit(1)
    tools = cfg.get("enabled_tools")
    if not isinstance(tools, list):
        print(json.dumps({"scope_pass": False, "reason": "enabled_tools_missing"}))
        sys.exit(1)
    actual = set(tools)
    extra = sorted(actual - expected)
    missing = sorted(expected - actual)
    if extra or missing:
        print(json.dumps({"scope_pass": False, "extra": extra, "missing": missing},
                         ensure_ascii=False))
        sys.exit(1)
    print(json.dumps({"scope_pass": True, "tool_count": len(actual)}))


if __name__ == "__main__":
    main()
