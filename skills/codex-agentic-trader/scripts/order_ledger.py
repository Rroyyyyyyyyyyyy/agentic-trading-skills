#!/usr/bin/env python3
"""Local Robinhood order ledger: ref_id -> broker order_id and lifecycle state.

This is a durable reconciliation projection, not a source of broker truth.  It stores no
account number.  Broker reads must still be refreshed before every decision.
"""
import argparse
import fcntl
import json
import math
import os
import sys
from datetime import datetime
from pathlib import Path

ACTIVE = {"new", "queued", "confirmed", "unconfirmed", "partially_filled",
          "pending_cancelled", "locating", "submission_unknown"}
TERMINAL = {"filled", "cancelled", "rejected", "failed", "voided",
            "partially_filled_rest_cancelled", "locate_failed"}
VALID = ACTIVE | TERMINAL


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(value)


def _validate_row(row):
    required_strings = ("decision_key", "ref_id", "symbol", "side", "asset_class",
                        "status", "created_at_et", "updated_at_et")
    if not isinstance(row, dict) or any(
            not isinstance(row.get(key), str) or not row[key] for key in required_strings):
        raise ValueError("invalid_order_ledger_row")
    if row["status"] not in VALID or row["side"] not in ("buy", "sell"):
        raise ValueError("invalid_order_ledger_row")
    if not (_finite(row.get("amount")) and row["amount"] > 0):
        raise ValueError("invalid_order_ledger_row")
    order_id = row.get("order_id")
    if row["status"] != "submission_unknown" and not (
            isinstance(order_id, str) and order_id):
        raise ValueError("invalid_order_ledger_row")
    if order_id is not None and not isinstance(order_id, str):
        raise ValueError("invalid_order_ledger_row")


def _read_locked(fh):
    fh.seek(0)
    raw = fh.read()
    data = json.loads(raw) if raw.strip() else {"version": 1, "orders": []}
    if data.get("version") != 1 or not isinstance(data.get("orders"), list):
        raise ValueError("order_ledger_corrupt")
    for row in data["orders"]:
        _validate_row(row)
    if len({row["ref_id"] for row in data["orders"]}) != len(data["orders"]):
        raise ValueError("duplicate_ref_id")
    order_ids = [row["order_id"] for row in data["orders"] if row.get("order_id")]
    if len(set(order_ids)) != len(order_ids):
        raise ValueError("duplicate_order_id")
    return data


def _mutate(path, callback):
    p = Path(path).expanduser(); p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600); os.fchmod(fd, 0o600)
    with os.fdopen(fd, "r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        data = _read_locked(fh)
        result = callback(data)
        fh.seek(0); fh.truncate()
        fh.write(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False))
        fh.flush(); os.fsync(fh.fileno())
        return result


def record_submission(path, event):
    def callback(data):
        if any(row["ref_id"] == event.get("ref_id") for row in data["orders"]):
            raise ValueError("duplicate_ref_id")
        now = event.get("observed_at_et")
        try:
            parsed = datetime.fromisoformat(now)
        except (TypeError, ValueError):
            raise ValueError("invalid_observed_at_et") from None
        if parsed.tzinfo is None:
            raise ValueError("invalid_observed_at_et")
        row = {
            "decision_key": event.get("decision_key"), "ref_id": event.get("ref_id"),
            "order_id": event.get("order_id"), "symbol": event.get("symbol"),
            "side": event.get("side"), "asset_class": event.get("asset_class"),
            "amount": event.get("amount"), "status": event.get("status"),
            "created_at_et": now, "updated_at_et": now,
        }
        _validate_row(row)
        if row.get("order_id") and any(
                existing.get("order_id") == row["order_id"] for existing in data["orders"]):
            raise ValueError("duplicate_order_id")
        data["orders"].append(row)
        return row
    return _mutate(path, callback)


def update_status(path, event):
    def callback(data):
        matches = [row for row in data["orders"] if row["ref_id"] == event.get("ref_id")]
        if len(matches) != 1:
            raise ValueError("ref_id_not_found")
        row = matches[0]
        new_status = event.get("status")
        if new_status not in VALID:
            raise ValueError("invalid_status")
        if row["status"] in TERMINAL and new_status != row["status"]:
            raise ValueError("terminal_state_is_immutable")
        order_id = event.get("order_id")
        if row.get("order_id") and order_id and row["order_id"] != order_id:
            raise ValueError("order_id_mismatch")
        if not row.get("order_id") and order_id:
            if any(existing.get("order_id") == order_id for existing in data["orders"]):
                raise ValueError("duplicate_order_id")
            row["order_id"] = order_id
        if new_status != "submission_unknown" and not row.get("order_id"):
            raise ValueError("order_id_required")
        observed = event.get("observed_at_et")
        try:
            parsed = datetime.fromisoformat(observed)
        except (TypeError, ValueError):
            raise ValueError("invalid_observed_at_et") from None
        if parsed.tzinfo is None:
            raise ValueError("invalid_observed_at_et")
        row["status"] = new_status; row["updated_at_et"] = observed
        return row
    return _mutate(path, callback)


def read_projection(path):
    p = Path(path).expanduser()
    if not p.exists():
        return []
    with open(p) as fh:
        fcntl.flock(fh, fcntl.LOCK_SH)
        data = _read_locked(fh)
    return [{"order_id": row.get("order_id") or f"unknown:{row['ref_id']}",
             "status": "unknown" if row["status"] == "submission_unknown" else row["status"]}
            for row in data["orders"]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--input")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("record-submission"); sub.add_parser("update-status"); sub.add_parser("projection")
    args = parser.parse_args()
    try:
        if args.command == "projection":
            result = {"ok": True, "local_intents": read_projection(args.ledger)}
        else:
            raw = Path(args.input).read_text() if args.input else sys.stdin.read()
            event = json.loads(raw)
            fn = record_submission if args.command == "record-submission" else update_status
            result = {"ok": True, "order": fn(args.ledger, event)}
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}:{exc}"}))
        sys.exit(1)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
