#!/usr/bin/env python3
"""链式交易/决策日志追加器。

- JSONL，每行 {seq, prev_hash, event, hash}；hash = sha256(seq|prev_hash|event 规范化 JSON)。
- 追加前校验整条既有链；断链即拒（防误改，不防有全文件写权限者的篡改）。
- 拦截敏感键与疑似完整账号（字符串、整数、可整除浮点均检查）。
- flock 排他 + fsync；任何失败非 0 退出——调用方必须停止流程。
"""
import argparse
import fcntl
import hashlib
import json
import math
import re
import sys
from pathlib import Path

SENSITIVE_KEYS = {"password", "token", "secret", "api_key", "apikey", "credential",
                  "account_number", "account_id", "ssn"}
ALLOWED_ACCOUNT_KEYS = {"account_ref_masked", "account_last4"}
ALLOWED_EVENT_TYPES = {
    "research", "decision", "review", "submission", "order_state",
    "fill", "cancel_request", "cancel_state", "halt", "daily_review",
    "reconciliation_mismatch",
}


def _canon(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _entry_hash(seq, prev_hash, event):
    return hashlib.sha256(f"{seq}|{prev_hash}|{_canon(event)}".encode()).hexdigest()


def _scan(obj, path="$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                lowered = k.lower()
                normalized = "".join(ch for ch in lowered if ch.isalnum())
                if (lowered in SENSITIVE_KEYS
                        or ("account" in normalized and lowered not in ALLOWED_ACCOUNT_KEYS)):
                    raise ValueError(f"sensitive_key:{path}.{k}")
            _scan(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _scan(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        # sha1/sha256 摘要（decision_key 等）豁免；原始串 ≥8 连续数字或剥离分隔符后 ≥10 即拒
        # （阈值 10 为放行 ISO 日期 2026-08-13 → 20260813 恰 8 位）
        if len(obj) in (40, 64) and all(c in "0123456789abcdef" for c in obj.lower()):
            return
        def _run(s, n):
            run = 0
            for ch in s + "\0":
                if ch.isdigit():
                    run += 1
                else:
                    if run >= n:
                        return True
                    run = 0
            return False
        stripped = obj
        for sep in (" ", "-", "_", "."):
            stripped = stripped.replace(sep, "")
        if _run(obj, 8) or _run(stripped, 10):
            raise ValueError(f"account_like_value:{path}")
    elif isinstance(obj, float):
        if not math.isfinite(obj):
            raise ValueError(f"non_finite_numeric_value:{path}")


def verify_chain(lines):
    prev = "GENESIS"
    for i, line in enumerate(lines):
        entry = json.loads(line)
        if entry["seq"] != i or entry["prev_hash"] != prev:
            raise ValueError(f"chain_broken_at:{i}")
        if _entry_hash(entry["seq"], entry["prev_hash"], entry["event"]) != entry["hash"]:
            raise ValueError(f"hash_mismatch_at:{i}")
        prev = entry["hash"]
    return prev, len(lines)


def append(log_path, event):
    _scan(event)
    if event.get("type") not in ALLOWED_EVENT_TYPES:
        raise ValueError("event_type_required")
    if "account_last4" in event and not re.fullmatch(r"\d{4}", str(event["account_last4"])):
        raise ValueError("account_last4_invalid")
    if "account_ref_masked" in event and not re.fullmatch(
            r"\*\*\*\*\d{4}", str(event["account_ref_masked"])):
        raise ValueError("account_ref_masked_invalid")
    p = Path(log_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        lines = [ln for ln in fh.read().splitlines() if ln.strip()]
        prev, seq = verify_chain(lines)
        entry = {"seq": seq, "prev_hash": prev, "event": event,
                 "hash": _entry_hash(seq, prev, event)}
        fh.write(_canon(entry) + "\n")
        fh.flush()
        import os
        os.fsync(fh.fileno())
    return entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", required=True, help="私有 JSONL 日志路径（不入 git）")
    ap.add_argument("--input", help="事件 JSON 文件；缺省读 stdin")
    args = ap.parse_args()
    try:
        raw = Path(args.input).read_text() if args.input else sys.stdin.read()
        event = json.loads(raw)
        entry = append(args.log, event)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}:{exc}"}, ensure_ascii=False))
        sys.exit(1)
    print(json.dumps({"ok": True, "seq": entry["seq"], "hash": entry["hash"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
