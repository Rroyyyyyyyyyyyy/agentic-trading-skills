#!/usr/bin/env python3
"""Robinhood supervised-execution deterministic gate.

在 decision_gate 全量基础检查之上扩展：
- mandate 推导 supervised/shadow；mandate 是必要但不充分的委托包络；
- HALT 哨兵：存在时只放行风险退出卖单，其余全拒；halt_required 时闸门自写 HALT（自锁）；
- 本地 intent 与券商订单按 Robinhood order_id 对账，不平拒新单；
- 两阶段：pre_review → post_review；验证 Robinhood review_equity_order 真实回执；
- 闸门本地状态账本（gate_state_file）：当日已批审核数/金额/decision_key/review_fingerprint 由闸门
  自己记账，与调用方声明取更严——频次/换手/幂等不再单信调用方（对抗审查 P2 修复）；
- limit_day 单必须携带数量与限价；approved_intent 与 review_fingerprint/decision_key 绑定。

当前发布版不把 mandate 当成逐笔确认：post_review 通过只会输出
can_request_confirmation=true；获得用户对该 review 的明确确认后才可调用 place_equity_order。
"""
import argparse
import fcntl
import hashlib
import json
import math
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SCRIPT_DIR = Path(__file__).resolve().parent
POLICY_PATH = SCRIPT_DIR.parent / "policy" / "policy.json"
TOOLSET_PATH = SCRIPT_DIR.parent / "policy" / "enabled_tools_live.json"
sys.path.insert(0, str(SCRIPT_DIR))

ACTIVE_ORDER_STATUSES = {"new", "queued", "confirmed", "unconfirmed", "partially_filled",
                         "pending_cancelled", "locating"}
TERMINAL_ORDER_STATUSES = {"filled", "cancelled", "rejected", "failed", "voided",
                           "partially_filled_rest_cancelled", "locate_failed"}
VALID_ORDER_STATUSES = ACTIVE_ORDER_STATUSES | TERMINAL_ORDER_STATUSES | {"unknown"}
MANDATE_CONFIRMATION_TEMPLATE = "I AUTHORIZE SUPERVISED ROBINHOOD TRADING {last4}"


def _fail(violations, phase="unknown"):
    print(json.dumps({
        "mode": "shadow", "phase": phase, "would_allow": False,
        "can_review": False, "can_request_confirmation": False,
        "can_submit": False, "halt_required": False,
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


def _parse_iso_datetime(s):
    """要求带时区偏移的 ISO 8601 时间；naive 值一律拒绝。"""
    if not isinstance(s, str):
        return None
    try:
        value = datetime.fromisoformat(s)
        return value if value.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _canonical_hash(obj):
    raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _now_et(policy):
    return datetime.now(ZoneInfo(policy["market_timezone"]))


def _minutes(hhmm):
    hour, minute = (int(part) for part in hhmm.split(":"))
    return hour * 60 + minute


def _check_execution_window(current, policy, prop, violations):
    et = current.astimezone(ZoneInfo(policy["market_timezone"]))
    minute = et.hour * 60 + et.minute
    if et.weekday() >= 5:
        violations.append("not_weekday_session")
        return
    if not (_minutes(policy["regular_session_start_et"])
            <= minute <= _minutes(policy["regular_session_end_et"])):
        violations.append("outside_regular_session")
    is_new_stock_risk = (prop.get("side") == "buy"
                         and prop.get("asset_class") == "stock")
    if is_new_stock_risk and minute < _minutes(policy["new_risk_not_before_et"]):
        violations.append("new_stock_risk_too_early")


def _check_snapshot_coverage(account, violations):
    coverage = account.get("coverage") if isinstance(account, dict) else None
    required = ("positions_complete", "open_equity_orders_complete",
                "today_equity_orders_complete", "today_fills_complete")
    if not isinstance(coverage, dict):
        violations.append("snapshot_coverage_missing")
        return
    for key in required:
        if coverage.get(key) is not True:
            violations.append(f"snapshot_coverage_incomplete:{key}")
    # 当前 Robinhood MCP 未公开 advanced-order 读取工具；这是必须显式携带的差距，
    # 不得把“工具不可用”写成“已确认为空”。平台/操作人已独立确认时才可 true。
    if coverage.get("advanced_orders_checked") is not True:
        violations.append("advanced_order_coverage_unverified")


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


def load_mandate(policy, now=None, skip_keychain=False):
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
    if m.get("mandate_version") != "2.0":
        return None, "mandate_version_unsupported"
    if not (isinstance(m.get("mandate_id"), str) and m["mandate_id"]):
        return None, "mandate_id_invalid"
    if m.get("account_ref_masked") != f"****{policy['account_last4']}":
        return None, "mandate_account_mismatch"
    expected = MANDATE_CONFIRMATION_TEMPLATE.format(last4=policy["account_last4"])
    if m.get("confirmation") != expected:
        return None, "mandate_confirmation_invalid"
    if m.get("execution_mode") not in policy["supported_execution_modes"]:
        return None, "mandate_execution_mode_unsupported"
    if m.get("requires_per_order_confirmation") is not True:
        return None, "mandate_confirmation_policy_invalid"
    issued = _parse_iso_datetime(m.get("issued_at_et"))
    not_before = _parse_iso_datetime(m.get("not_before_et"))
    expires = _parse_iso_datetime(m.get("expires_at_et"))
    current = now or _now_et(policy)
    if issued is None or not_before is None or expires is None:
        return None, "mandate_time_invalid"
    issued = issued.astimezone(ZoneInfo(policy["market_timezone"]))
    not_before = not_before.astimezone(ZoneInfo(policy["market_timezone"]))
    expires = expires.astimezone(ZoneInfo(policy["market_timezone"]))
    current = current.astimezone(ZoneInfo(policy["market_timezone"]))
    if not_before < issued or expires <= not_before:
        return None, "mandate_time_invalid"
    if expires - issued > timedelta(days=policy["max_mandate_days"]):
        return None, "mandate_duration_exceeded"
    if current < not_before or current > expires:
        return None, "mandate_expired"
    for f in ("max_order_amount", "max_daily_turnover", "max_orders_per_day"):
        if not _is_finite_number(m.get(f)) or m[f] <= 0:
            return None, f"mandate_invalid_{f}"
    if m["max_order_amount"] > policy["capital_cap"]:
        return None, "mandate_order_amount_exceeds_policy"
    if m["max_daily_turnover"] > policy["max_daily_turnover_ratio"] * policy["capital_cap"]:
        return None, "mandate_turnover_exceeds_policy"
    if int(m["max_orders_per_day"]) != m["max_orders_per_day"] \
            or m["max_orders_per_day"] > policy["max_orders_per_day"]:
        return None, "mandate_orders_exceed_policy"
    if m.get("policy_sha256") != _canonical_hash(policy):
        return None, "mandate_policy_drift"
    try:
        toolset = json.loads(TOOLSET_PATH.read_text())
    except Exception:  # noqa: BLE001
        return None, "mandate_toolset_unreadable"
    if m.get("toolset_sha256") != _canonical_hash(toolset):
        return None, "mandate_toolset_drift"
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
                    and isinstance(o.get("review_fingerprint"), str)
                    and isinstance(o.get("ref_id"), str)
                    and _is_finite_number(o.get("amount"))):
                return None, "state_corrupt"
        return s, "ok"
    except Exception:  # noqa: BLE001
        return None, "state_corrupt"


def save_state(path, state):
    p = _expand(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    import os
    fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "r+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps(state, ensure_ascii=False))
        fh.flush()
        os.fsync(fh.fileno())


def record_submission(state, decision_key, review_fingerprint, amount, ref_id):
    state["orders"].append({"decision_key": decision_key,
                            "review_fingerprint": review_fingerprint,
                            "ref_id": ref_id, "amount": amount})
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
    def _valid_local(row):
        return (isinstance(row, dict)
                and isinstance(row.get("order_id"), str) and bool(row["order_id"])
                and row.get("status") in VALID_ORDER_STATUSES)

    def _valid_broker(row):
        return (isinstance(row, dict)
                and isinstance(row.get("order_id"), str) and bool(row["order_id"])
                and isinstance(row.get("placed_agent"), str) and bool(row["placed_agent"])
                and row.get("status") in VALID_ORDER_STATUSES)

    if not all(_valid_local(r) for r in intents) or not all(_valid_broker(r) for r in broker):
        violations.append("invalid_reconciliation_entry")
        return
    broker_by_key = {}
    for r in broker:
        k = r["order_id"]
        if k in broker_by_key and broker_by_key[k]["status"] != r["status"]:
            violations.append("reconciliation_mismatch")
            return
        broker_by_key[k] = r
    local_by_key = {}
    for it in intents:
        if it["order_id"] in local_by_key and local_by_key[it["order_id"]]["status"] != it["status"]:
            violations.append("reconciliation_mismatch")
            return
        local_by_key[it["order_id"]] = it
        if it["status"] == "unknown":
            violations.append("reconciliation_mismatch")
            return
        if it["status"] in ACTIVE_ORDER_STATUSES and it["order_id"] not in broker_by_key:
            violations.append("reconciliation_mismatch")
            return
    for br in broker:
        if br["status"] == "unknown":
            violations.append("reconciliation_mismatch")
            return
        if br["status"] in ACTIVE_ORDER_STATUSES and br["placed_agent"] != "agentic":
            violations.append("external_open_order_present")
            return
        if br["placed_agent"] == "agentic" and br["order_id"] not in local_by_key:
            violations.append("reconciliation_mismatch")
            return


def _decimal_string(value):
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _review_fingerprint(review):
    return _canonical_hash(review)


def check_review(review, policy, prop, used_fingerprints, violations, now=None):
    """验证当前 Robinhood review_equity_order 的请求+响应包络。"""
    if not isinstance(review, dict):
        violations.append("missing_review")
        return None
    request = review.get("request")
    response = review.get("response")
    observed = _parse_iso_datetime(review.get("observed_at_et"))
    if not isinstance(request, dict) or not isinstance(response, dict) or observed is None:
        violations.append("invalid_review_envelope")
        return None
    current = now or _now_et(policy)
    age = (current.astimezone(observed.tzinfo) - observed).total_seconds()
    if not (-policy["max_clock_skew_seconds"] <= age <= policy["max_review_age_seconds"]):
        violations.append("review_stale")

    if request.get("market_hours") != "regular_hours" or request.get("time_in_force") != "gfd":
        violations.append("review_session_forbidden")
    expected_type = "market" if prop.get("order_type") == "market_day" else "limit"
    for field, expected in (("symbol", prop.get("symbol")), ("side", prop.get("side")),
                            ("type", expected_type)):
        if request.get(field) != expected or response.get(field) != expected:
            violations.append("review_proposal_mismatch")

    if expected_type == "market":
        reviewed_amount = _decimal_string(request.get("dollar_amount"))
        echoed_amount = _decimal_string(response.get("dollar_amount"))
        if (reviewed_amount is None or echoed_amount is None
                or not _is_finite_number(prop.get("amount"))
                or abs(reviewed_amount - prop["amount"]) > 0.01
                or abs(echoed_amount - prop["amount"]) > 0.01
                or "quantity" in request):
            violations.append("review_proposal_mismatch")
    else:
        reviewed_qty = _decimal_string(request.get("quantity"))
        echoed_qty = _decimal_string(response.get("quantity"))
        reviewed_limit = _decimal_string(request.get("limit_price"))
        echoed_limit = _decimal_string(response.get("limit_price"))
        if (reviewed_qty is None or reviewed_qty <= 0 or echoed_qty != reviewed_qty
                or reviewed_limit is None or reviewed_limit <= 0 or echoed_limit != reviewed_limit
                or not _is_finite_number(prop.get("quantity"))
                or abs(reviewed_qty - prop["quantity"]) > 1e-6
                or abs(reviewed_limit - prop.get("limit_price", 0)) > 0.000001
                or abs(reviewed_qty * reviewed_limit - prop.get("amount", 0)) > 0.01
                or "dollar_amount" in request):
            violations.append("review_proposal_mismatch")

    if response.get("order_checks") != {}:
        violations.append("review_has_alerts")
    disclosure = response.get("market_data_disclosure")
    if not (isinstance(disclosure, str) and disclosure.strip()):
        violations.append("review_disclosure_missing")
    quote = response.get("quote_data")
    if not isinstance(quote, dict):
        violations.append("review_quote_missing")
    else:
        bid = _decimal_string(quote.get("bid_price"))
        ask = _decimal_string(quote.get("ask_price"))
        if (quote.get("symbol") != prop.get("symbol") or quote.get("state") != "active"
                or quote.get("has_traded") is not True or bid is None or ask is None
                or bid <= 0 or ask <= 0 or ask < bid):
            violations.append("review_quote_invalid")
        else:
            midpoint = (bid + ask) / 2
            spread_bps = (ask - bid) / midpoint * 10000
            if spread_bps > policy["max_spread_bps"]:
                violations.append("review_spread_too_wide")
    fingerprint = _review_fingerprint(review)
    if fingerprint in used_fingerprints:
        violations.append("review_replayed")
    return fingerprint


def evaluate(payload, policy, now=None, skip_keychain=False, state=None):
    import decision_gate

    phase = payload.get("phase")
    if phase not in ("pre_review", "post_review"):
        return {"mode": "shadow", "phase": str(phase), "would_allow": False,
                "can_review": False, "can_request_confirmation": False,
                "can_submit": False, "halt_required": False,
                "violations": ["invalid_phase"]}
    if "mode" in payload:
        return {"mode": "shadow", "phase": phase, "would_allow": False,
                "can_review": False, "can_request_confirmation": False,
                "can_submit": False, "halt_required": False,
                "violations": ["mode_must_not_be_declared"]}

    halted = check_halt(policy)
    mandate, mandate_reason = load_mandate(policy, now=now, skip_keychain=skip_keychain)
    mode = "supervised" if mandate else "shadow"

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
    used_review_fingerprints = set(
        o.get("review_fingerprint") for o in state_orders if o.get("review_fingerprint"))
    caller_fingerprints = hist.get("used_review_fingerprints")
    if isinstance(caller_fingerprints, list):
        used_review_fingerprints.update(
            p for p in caller_fingerprints if isinstance(p, str))

    # 基础检查复用 decision_gate
    base_payload = dict(payload)
    base_payload["mode"] = "decide"
    base_payload["history"] = merged_hist
    base_payload.pop("phase", None)
    recon_sub = base_payload.pop("reconciliation", None)
    review_sub = base_payload.pop("review", None)
    current = now or _now_et(policy)
    base = decision_gate.evaluate(base_payload, policy, now=current)
    violations += list(base["violations"])

    # P6 修复：被 pop 的子树也要过账号泄漏扫描
    decision_gate._scan_account_like({"reconciliation": recon_sub, "review": review_sub},
                                     violations)

    _check_snapshot_coverage(payload.get("account"), violations)
    _check_execution_window(current, policy, prop, violations)

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
        orders_today = merged_hist.get("orders_today")
        if _is_finite_number(orders_today) and orders_today >= mandate["max_orders_per_day"]:
            violations.append("mandate_daily_order_limit_reached")

    # 对账
    check_reconciliation(recon_sub, violations)

    # HALT：存在时只放行风险退出卖单（hard-boundaries §4 清仓通道）
    if halted and not is_risk_exit:
        violations.append("halt_active")

    # post_review 校验（与当前 Robinhood review 工具契约一致）
    review_fingerprint = None
    if phase == "post_review":
        review_fingerprint = check_review(
            review_sub, policy, prop, used_review_fingerprints, violations, now=now)

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
        "can_review": False,
        "can_request_confirmation": False,
        "can_submit": False,
    }
    if mandate:
        result["derived"]["mandate_expires_at_et"] = mandate["expires_at_et"]
    if mode == "shadow":
        result["shadow_would_allow"] = would_allow
        return result
    if would_allow and phase == "pre_review":
        result["can_review"] = True
    if would_allow and phase == "post_review":
        ref_id = str(uuid.uuid4())
        result["can_request_confirmation"] = True
        result["approved_intent"] = {
            "symbol": prop.get("symbol"),
            "side": prop.get("side"),
            "amount": amount,
            "asset_class": prop.get("asset_class"),
            "order_type": prop.get("order_type"),
            "limit_price": limit_price,
            "trigger": prop.get("trigger"),
            "decision_key": base.get("decision_key"),
            "quantity": prop.get("quantity"),
            "market_hours": "regular_hours",
            "time_in_force": "gfd",
            "review_fingerprint": review_fingerprint,
            "ref_id": ref_id,
            "requires_explicit_confirmation": True,
        }
        result["required_market_data_disclosure"] = (review_sub or {}).get(
            "response", {}).get("market_data_disclosure")
    return result


def check_runtime(policy):
    halted = check_halt(policy)
    mandate, reason = load_mandate(policy)
    report = {
        "halt_active": halted,
        "mandate_status": reason,
        "mode_if_run_now": "supervised" if mandate and not halted else "shadow",
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

    # CLI 运行不信任调用方自报的 local_intents；以本地持久 ledger 为准。
    try:
        import order_ledger
        recon = payload.get("reconciliation")
        if not isinstance(recon, dict):
            _fail(["missing_reconciliation"], phase=str(payload.get("phase")))
        recon["local_intents"] = order_ledger.read_projection(policy["order_ledger_file"])
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        _fail([f"order_ledger_error:{type(exc).__name__}"], phase=str(payload.get("phase")))

    date_et = str(payload.get("as_of_et", ""))[:10]
    state_path = _expand(policy["gate_state_file"])
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lock_fh:
        # 必须把 load + evaluate + reserve 放在同一把锁里，否则并发唤醒可绕过频次门。
        fcntl.flock(lock_fh, fcntl.LOCK_EX)
        state, state_status = load_state(policy["gate_state_file"], date_et)
        if state_status != "ok":
            _fail(["gate_state_corrupt"], phase=str(payload.get("phase")))
        try:
            result = evaluate(payload, policy, state=state)
        except Exception as exc:  # noqa: BLE001
            _fail([f"gate_internal_error:{type(exc).__name__}"], phase=str(payload.get("phase")))

        if result.get("halt_required"):
            try:
                write_halt(policy)
            except Exception:  # noqa: BLE001
                result["violations"].append("halt_write_failed")
                result["would_allow"] = False
                result["can_review"] = False
                result["can_request_confirmation"] = False
                result["can_submit"] = False
        # post_review 批准时预留 ref_id 并消耗该回执。用户最后放弃也保守计入当日频次。
        if result.get("can_request_confirmation"):
            try:
                intent = result["approved_intent"]
                record_submission(state, result["decision_key"],
                                  intent["review_fingerprint"], intent["amount"],
                                  intent["ref_id"])
                save_state(policy["gate_state_file"], state)
            except Exception:  # noqa: BLE001
                result["violations"].append("gate_state_write_failed")
                result["would_allow"] = False
                result["can_request_confirmation"] = False
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["would_allow"] else 1)


if __name__ == "__main__":
    main()
