from .domain import ConflictError, DomainError, NotFoundError, number, require_text

BACKUP_SOURCE_ROLES = {"coordinator", "regulator"}
DISPATCH_CREATE_ROLES = {"dispatcher", "coordinator"}
DISPATCH_ACTION_ROLES = {
    "deliver": {"field_operator", "dispatcher"},
    "fail": {"field_operator", "dispatcher"},
    "recheck": {"lab", "analyst"},
    "revoke": {"coordinator", "dispatcher"},
    "redeliver": {"field_operator", "dispatcher"},
}
TERMINAL_STATUS = {"released", "revoked"}
ITEM_CLOSED_STATUS = {"cancelled", "restored"}


def round3(value):
    return round(float(value), 3)


def normalize_backup_source(payload):
    capacity = number(payload, "capacity", 0)
    if capacity <= 0:
        raise DomainError("invalid_number", "capacity 必须大于 0")
    return {
        "source_code": require_text(payload, "source_code"),
        "name": require_text(payload, "name"),
        "capacity": round3(capacity),
    }


def normalize_order(payload):
    source_code = require_text(payload, "source_code")
    zones = payload.get("zones")
    if not isinstance(zones, list) or not zones:
        raise DomainError("zones_required", "至少需要一个送水片区")
    requests = []
    seen = set()
    for entry in zones:
        if not isinstance(entry, dict):
            raise DomainError("invalid_zones", "片区请求必须是对象列表")
        zone_id = entry.get("zone_id")
        if not isinstance(zone_id, str) or not zone_id.strip():
            raise DomainError("invalid_zones", "片区编号不能为空")
        zone_id = zone_id.strip()
        if zone_id in seen:
            raise DomainError("duplicate_zone", "同一调度单中片区不能重复", 409)
        seen.add(zone_id)
        amount = number(entry, "amount", 0)
        if amount <= 0:
            raise DomainError("invalid_number", "片区水量必须大于 0")
        requests.append({"zone_id": zone_id, "amount": round3(amount)})
    request_id = payload.get("request_id")
    if request_id is not None:
        if not isinstance(request_id, str) or not request_id.strip():
            raise DomainError("field_required", "request_id 不能为空")
        request_id = request_id.strip()
    note = payload.get("note", "")
    if not isinstance(note, str):
        raise DomainError("invalid_note", "note 必须是字符串")
    return {"source_code": source_code, "requests": requests, "request_id": request_id, "note": note.strip()}


def adjust_requests(requests, remaining):
    """按请求顺序预留水量，后到的请求按剩余份额改单。"""
    adjusted = []
    left = round3(remaining)
    for request in requests:
        reserved = round3(min(request["amount"], max(left, 0.0)))
        left = round3(left - reserved)
        adjusted.append({"zone_id": request["zone_id"], "requested": request["amount"], "reserved": reserved})
    return adjusted


def derive_status(reservations, current):
    if current in TERMINAL_STATUS:
        return current
    if any(reservation["status"] == "pending" for reservation in reservations):
        if any(reservation["status"] != "pending" for reservation in reservations):
            return "partial"
        return "reserved"
    return "completed"


def _find_reservation(reservations, zone_id):
    for reservation in reservations:
        if reservation["zone_id"] == zone_id:
            return reservation
    raise NotFoundError("zone_not_found", "调度单中不存在该片区")


def _need_open_order(order):
    if order["status"] in TERMINAL_STATUS:
        raise ConflictError("order_closed", "调度单已关闭，不能执行该操作")


def _release_pending(order, reservations, kind, detail):
    ledger = []
    updates = []
    for reservation in reservations:
        if reservation["status"] != "pending":
            continue
        updates.append({"id": reservation["id"], "status": "released", "delivered": reservation["delivered"]})
        if reservation["reserved"] > 0:
            ledger.append({
                "kind": kind,
                "zone_id": reservation["zone_id"],
                "amount": reservation["reserved"],
                "detail": detail,
            })
    return updates, ledger


def plan_action(action, order, reservations, todos, payload, remaining):
    if action == "deliver":
        return _plan_deliver(order, reservations, payload)
    if action == "fail":
        return _plan_fail(order, reservations, payload)
    if action == "recheck":
        return _plan_recheck(order, reservations, payload)
    if action == "revoke":
        return _plan_revoke(order, reservations, payload)
    if action == "redeliver":
        return _plan_redeliver(order, reservations, todos, payload, remaining)
    raise DomainError("unknown_action", "不支持的调度操作")


def _plan_deliver(order, reservations, payload):
    _need_open_order(order)
    zone_id = require_text(payload, "zone_id")
    reservation = _find_reservation(reservations, zone_id)
    if reservation["status"] != "pending":
        raise ConflictError("not_pending", "该片区的预留已处理")
    amount = reservation["reserved"]
    if amount <= 0:
        raise ConflictError("nothing_to_deliver", "该片区没有可送达的预留水量")
    updates = [{"id": reservation["id"], "status": "delivered", "delivered": amount}]
    ledger = [{"kind": "deliver", "zone_id": zone_id, "amount": amount, "detail": "片区送水送达"}]
    return _plan(order, reservations, updates, ledger, {"zone_id": zone_id, "amount": amount})


def _plan_fail(order, reservations, payload):
    _need_open_order(order)
    zone_id = require_text(payload, "zone_id")
    reservation = _find_reservation(reservations, zone_id)
    if reservation["status"] != "pending":
        raise ConflictError("not_pending", "该片区的预留已处理")
    delivered = number(payload, "delivered_amount", 0) if "delivered_amount" in payload else 0.0
    delivered = round3(delivered)
    if delivered < 0 or delivered >= reservation["reserved"]:
        raise DomainError("invalid_delivery", "已送达部分必须大于等于 0 且小于预留量")
    refund = round3(reservation["reserved"] - delivered)
    updates = [{"id": reservation["id"], "status": "failed", "delivered": delivered}]
    ledger = []
    if delivered > 0:
        ledger.append({"kind": "deliver", "zone_id": zone_id, "amount": delivered, "detail": "片区部分送达"})
    if refund > 0:
        ledger.append({"kind": "refund", "zone_id": zone_id, "amount": refund, "detail": "送水失败，预留退回"})
    new_todos = [{"zone_id": zone_id, "amount": refund}] if refund > 0 else []
    return _plan(
        order, reservations, updates, ledger,
        {"zone_id": zone_id, "delivered": delivered, "refunded": refund},
        new_todos=new_todos,
    )


def _plan_recheck(order, reservations, payload):
    _need_open_order(order)
    result = require_text(payload, "result")
    if result not in ("clear", "contaminated"):
        raise DomainError("invalid_result", "复检结果必须是 clear 或 contaminated")
    note = payload.get("note", "")
    if result == "clear":
        ledger = [{"kind": "recheck", "zone_id": None, "amount": 0, "detail": note or "复检合格"}]
        return _plan(order, reservations, [], ledger, {"result": result, "note": note})
    updates, ledger = _release_pending(order, reservations, "release", "复检发现污染，未执行预留释放")
    return _plan(
        order, reservations, updates, ledger,
        {"result": result, "note": note, "released": len(updates)},
        status="released", cancel_open_todos=True,
    )


def _plan_revoke(order, reservations, payload):
    _need_open_order(order)
    reason = require_text(payload, "reason")
    updates, ledger = _release_pending(order, reservations, "release", "调度单撤销：%s" % reason)
    return _plan(
        order, reservations, updates, ledger,
        {"reason": reason, "released": len(updates)},
        status="revoked", cancel_open_todos=True,
    )


def _plan_redeliver(order, reservations, todos, payload, remaining):
    _need_open_order(order)
    raw_id = payload.get("todo_id")
    if isinstance(raw_id, bool):
        raise DomainError("invalid_number", "todo_id 必须是数字")
    try:
        todo_id = int(raw_id)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "todo_id 必须是数字")
    todo = None
    for candidate in todos:
        if candidate["id"] == todo_id:
            todo = candidate
            break
    if todo is None:
        raise NotFoundError("todo_not_found", "补送待办不存在")
    if todo["status"] != "open":
        raise ConflictError("todo_closed", "补送待办已处理")
    granted = round3(min(todo["amount"], max(round3(remaining), 0.0)))
    if granted <= 0:
        raise ConflictError("no_remaining_water", "备用水源剩余水量不足，无法补送")
    reservation = _find_reservation(reservations, todo["zone_id"])
    delivered = round3(reservation["delivered"] + granted)
    updates = [{"id": reservation["id"], "status": "delivered", "delivered": delivered}]
    detail = "补送送达"
    if granted < todo["amount"]:
        detail = "剩余水量不足，补送部分送达（待办 %s，实送 %s）" % (todo["amount"], granted)
    ledger = [{"kind": "redeliver", "zone_id": todo["zone_id"], "amount": granted, "detail": detail}]
    return _plan(
        order, reservations, updates, ledger,
        {"todo_id": todo_id, "zone_id": todo["zone_id"], "amount": granted},
        done_todos=[todo_id],
    )


def _plan(order, reservations, updates, ledger, event, status=None, new_todos=None, done_todos=None, cancel_open_todos=False):
    merged = []
    by_id = {update["id"]: update for update in updates}
    for reservation in reservations:
        if reservation["id"] in by_id:
            merged.append(by_id[reservation["id"]])
        else:
            merged.append(reservation)
    return {
        "status": status or derive_status(merged, order["status"]),
        "reservations": updates,
        "ledger": ledger,
        "new_todos": new_todos or [],
        "done_todos": done_todos or [],
        "cancel_open_todos": cancel_open_todos,
        "event": event,
    }
