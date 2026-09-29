from datetime import datetime, timezone
from uuid import uuid4

from .domain import DomainError, number

ENTITY_TYPE = "water_contamination"
INITIAL_STATUS = "detected"
CREATE_ROLES = {"analyst", "dispatcher"}
SOURCE_ROLES = {"analyst", "dispatcher", "field_operator", "lab"}
BACKUP_SOURCE_ROLES = {"coordinator", "dispatcher"}
ACTION_ROLES = {
    "verify": {"analyst", "dispatcher"},
    "advise": {"coordinator", "dispatcher"},
    "switch_source": {"coordinator"},
    "submit_dispatch": {"coordinator", "dispatcher"},
    "report_delivery": {"field_operator", "dispatcher"},
    "complete_todo": {"dispatcher", "coordinator", "field_operator"},
    "cancel_dispatch": {"coordinator", "dispatcher"},
    "redeliver": {"coordinator", "dispatcher"},
    "flush": {"field_operator"},
    "disinfect": {"field_operator"},
    "sample": {"lab", "field_operator"},
    "restore": {"coordinator", "regulator"},
    "cancel": {"coordinator"},
}
ENFORCE_REGION = False
REGION_SENSITIVE_ACTIONS = set()
ACTION_REQUIRES_VERSION = {
    "advise",
    "switch_source",
    "submit_dispatch",
    "report_delivery",
    "complete_todo",
    "cancel_dispatch",
    "redeliver",
    "flush",
    "disinfect",
    "sample",
    "restore",
    "cancel",
}


def assess(payload):
    concentration = float(payload.get("concentration", 0))
    limit = max(float(payload.get("limit", 0.000001)), 0.000001)
    ratio = concentration / limit
    population = int(payload.get("population", 0))
    score = min(100.0, ratio * 35.0 + min(population / 1000.0, 40.0))
    if score >= 80:
        level = "critical"
    elif score >= 50:
        level = "high"
    elif score >= 20:
        level = "medium"
    else:
        level = "low"
    return {"score": round(score, 2), "level": level, "ratio": round(ratio, 3)}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix):
    return "%s-%s" % (prefix, uuid4().hex[:12])


def _water(payload):
    return payload.get("water_supply") or {
        "orders": [],
        "returns": [],
        "todos": [],
    }


def _find_order(water, order_id):
    for order in water.get("orders", []):
        if order.get("order_id") == order_id:
            return order
    return None


def _active_order(order):
    return order.get("status") in {"reserved", "partially_delivered", "failed"}


def _order_totals(order):
    requested = reserved = delivered = returned = 0.0
    for line in order.get("lines", []):
        requested += float(line.get("requested_volume", 0))
        reserved += float(line.get("reserved_volume", 0))
        delivered += float(line.get("delivered_volume", 0))
        returned += float(line.get("returned_volume", 0))
    return {
        "requested_volume": round(requested, 6),
        "reserved_volume": round(reserved, 6),
        "delivered_volume": round(delivered, 6),
        "returned_volume": round(returned, 6),
    }


def _refresh_order(order):
    statuses = {line.get("status") for line in order.get("lines", [])}
    if statuses == {"delivered"}:
        order["status"] = "completed"
    elif "cancelled" in statuses and not any(item in {"reserved", "partial", "partially_delivered"} for item in statuses):
        order["status"] = "cancelled"
    elif statuses == {"released"} or (statuses and all(item in {"released", "unfilled", "delivered"} for item in statuses) and "released" in statuses):
        order["status"] = "released"
    elif statuses == {"unfilled"}:
        order["status"] = "unfilled"
    elif any(item in {"reserved", "partial", "partially_delivered"} for item in statuses):
        order["status"] = "reserved"
    elif "failed" in statuses:
        order["status"] = "failed"
    else:
        order["status"] = "reserved"
    order.update(_order_totals(order))


def _return_record(order_id, zone_id, source_id, volume, reason, actor, record_type="return"):
    return {
        "record_id": _new_id("RET"),
        "type": record_type,
        "order_id": order_id,
        "zone_id": zone_id,
        "source_id": source_id,
        "volume": round(float(volume), 6),
        "reason": reason,
        "actor": actor,
        "created_at": _now(),
    }


def _todo_record(order_id, zone_id, source_id, volume, reason, kind="redelivery"):
    return {
        "todo_id": _new_id("TODO"),
        "kind": kind,
        "order_id": order_id,
        "zone_id": zone_id,
        "source_id": source_id,
        "volume": round(float(volume), 6),
        "reason": reason,
        "status": "open",
        "created_at": _now(),
        "completed_at": None,
        "completed_by": None,
    }


def submit_dispatch(current, payload, source, actor):
    source_id = payload["source_id"]
    if source is None:
        raise DomainError("backup_source_not_found", "备用水源不存在", 404)
    water = _water(current)
    order_id = payload.get("order_id") or _new_id("ORD")
    if _find_order(water, order_id) is not None:
        raise DomainError("duplicate_dispatch_order", "同一调度单编号不能重复提交", 409)

    available = round(float(source.get("available_volume", source["capacity_volume"])), 6)

    lines = []
    adjustments = []
    for zone in payload["zones"]:
        requested = round(float(zone["requested_volume"]), 6)
        allocated = min(requested, max(available, 0.0))
        allocated = round(allocated, 6)
        if allocated >= requested:
            status = "reserved"
        elif allocated > 0:
            status = "partial"
        else:
            status = "unfilled"
        line = {
            "zone_id": zone["zone_id"],
            "requested_volume": requested,
            "reserved_volume": allocated,
            "delivered_volume": 0.0,
            "returned_volume": 0.0,
            "status": status,
        }
        lines.append(line)
        if allocated < requested:
            adjustments.append({
                "record_id": _new_id("ADJ"),
                "zone_id": zone["zone_id"],
                "requested_volume": requested,
                "reserved_volume": allocated,
                "shortage_volume": round(requested - allocated, 6),
                "reason": "remaining_capacity",
                "created_at": _now(),
            })
        available = round(available - allocated, 6)

    order = {
        "order_id": order_id,
        "source_id": source_id,
        "requested_at": _now(),
        "requested_by": actor,
        "status": "reserved",
        "lines": lines,
        "adjustments": adjustments,
    }
    _refresh_order(order)
    water.setdefault("orders", []).append(order)
    current["water_supply"] = water
    current["alternate_source_id"] = source_id
    event = {
        "order_id": order_id,
        "source_id": source_id,
        "capacity_volume": source["capacity_volume"],
        "available_before": available + order["reserved_volume"],
        "available_after": available,
        "requested_volume": order["requested_volume"],
        "reserved_volume": order["reserved_volume"],
        "lines": lines,
        "adjustments": adjustments,
    }
    return "dispatch_reserved", current, event


def report_delivery(current, payload, actor, status):
    water = _water(current)
    order = _find_order(water, _text(payload, "order_id"))
    if order is None:
        raise DomainError("dispatch_order_not_found", "调度单不存在", 404)
    if not _active_order(order):
        raise DomainError("invalid_dispatch_state", "当前调度单状态不允许回报送水结果", 409)
    zone_id = _text(payload, "zone_id")
    matching_lines = [item for item in order.get("lines", []) if item.get("zone_id") == zone_id]
    if not matching_lines:
        raise DomainError("zone_not_in_order", "该片区不在调度单中", 404)
    line = next(
        (item for item in reversed(matching_lines) if item.get("status") in {"reserved", "partial", "partially_delivered"}),
        matching_lines[-1],
    )
    if line.get("status") not in {"reserved", "partial", "partially_delivered"}:
        raise DomainError("zone_not_pending", "该片区预留已结束，不能继续送水", 409)

    delivered_volume = number(payload, "delivered_volume", 0)
    outstanding = float(line["reserved_volume"]) - float(line["delivered_volume"])
    if delivered_volume > outstanding:
        raise DomainError("delivery_exceeds_reservation", "送达水量不能超过尚未送达的预留水量", 409)

    failed = bool(payload.get("failed", False))
    if not failed and delivered_volume <= 0:
        raise DomainError("delivery_volume_required", "成功送水必须上报正水量")
    delivered_volume = round(delivered_volume, 6)
    line["delivered_volume"] = round(float(line["delivered_volume"]) + delivered_volume, 6)
    delivery = {
        "record_id": _new_id("DEL"),
        "order_id": order["order_id"],
        "zone_id": zone_id,
        "source_id": order["source_id"],
        "volume": delivered_volume,
        "status": "failed" if failed else "delivered",
        "actor": actor,
        "created_at": _now(),
    }
    water.setdefault("returns", [])
    water.setdefault("todos", [])
    event = {"order_id": order["order_id"], "zone_id": zone_id, "delivery": delivery}

    if failed:
        remaining = round(float(line["reserved_volume"]) - float(line["delivered_volume"]), 6)
        if remaining <= 0:
            raise DomainError("failed_delivery_requires_remaining", "失败回报必须保留未送达水量", 409)
        reason = _text(payload, "reason")
        line["returned_volume"] = round(remaining, 6)
        line["status"] = "failed"
        water["returns"].append(_return_record(order["order_id"], zone_id, order["source_id"], remaining, reason, actor))
        todo = _todo_record(order["order_id"], zone_id, order["source_id"], remaining, reason)
        water["todos"].append(todo)
        original_todo_id = line.get("redelivery_for_todo_id")
        if original_todo_id:
            original_todo = next(
                (item for item in water["todos"] if item.get("todo_id") == original_todo_id), None
            )
            if original_todo and original_todo.get("status") == "scheduled":
                original_todo["status"] = "open"
                original_todo["completed_at"] = None
                original_todo["completed_by"] = None
        delivery["reason"] = reason
        event["returned_volume"] = remaining
        event["todo"] = todo
    elif round(float(line["delivered_volume"]), 6) >= round(float(line["reserved_volume"]), 6):
        line["status"] = "delivered"
        todo_id = line.get("redelivery_for_todo_id")
        if todo_id:
            todo = next((item for item in water.get("todos", []) if item.get("todo_id") == todo_id), None)
            if todo and todo.get("status") == "scheduled":
                todo["status"] = "completed"
                todo["completed_at"] = _now()
                todo["completed_by"] = actor
                original_line = next(
                    (item for item in order.get("lines", [])
                     if item.get("status") == "failed" and item.get("zone_id") == zone_id),
                    None,
                )
                if original_line:
                    original_line["status"] = "delivered"
    else:
        line["status"] = "partially_delivered"
    if delivered_volume > 0:
        water.setdefault("deliveries", []).append(delivery)
    _refresh_order(order)
    current["water_supply"] = water
    return status, current, event


def redeliver(current, payload, source, actor):
    water = _water(current)
    todo_id = _text(payload, "todo_id")
    todo = next((item for item in water.setdefault("todos", []) if item.get("todo_id") == todo_id), None)
    if todo is None:
        raise DomainError("todo_not_found", "补送待办不存在", 404)
    if todo.get("status") != "open":
        raise DomainError("todo_closed", "补送待办已关闭", 409)
    order = _find_order(water, todo.get("order_id"))
    if order is None:
        raise DomainError("dispatch_order_not_found", "调度单不存在", 404)
    source_id = source.get("source_id") if source else todo.get("source_id")
    if source is None:
        raise DomainError("backup_source_not_found", "备用水源不存在", 404)
    requested = round(float(todo.get("volume", 0)), 6)
    available = float(source.get("available_volume", source["capacity_volume"]))
    allocated = round(min(requested, max(available, 0.0)), 6)
    new_line = {
        "zone_id": todo["zone_id"],
        "requested_volume": requested,
        "reserved_volume": allocated,
        "delivered_volume": 0.0,
        "returned_volume": 0.0,
        "status": "reserved" if allocated >= requested else ("partial" if allocated > 0 else "unfilled"),
        "redelivery_for_todo_id": todo_id,
    }
    order.setdefault("lines", []).append(new_line)
    adjustment = {
        "record_id": _new_id("ADJ"),
        "zone_id": todo["zone_id"],
        "requested_volume": requested,
        "reserved_volume": allocated,
        "shortage_volume": round(requested - allocated, 6),
        "reason": "redelivery_capacity",
        "todo_id": todo_id,
        "created_at": _now(),
    }
    order.setdefault("adjustments", []).append(adjustment)
    if allocated > 0:
        water.setdefault("returns", []).append({
            "record_id": _new_id("SUP"),
            "type": "supplement_reservation",
            "order_id": order["order_id"],
            "zone_id": todo["zone_id"],
            "source_id": source_id,
            "volume": allocated,
            "reason": "redelivery",
            "todo_id": todo_id,
            "actor": actor,
            "created_at": _now(),
        })
    if allocated > 0:
        todo["status"] = "scheduled"
    else:
        shortage_todo = _todo_record(
            order["order_id"], todo["zone_id"], source_id, requested, "redelivery_no_capacity"
        )
        water.setdefault("todos", []).append(shortage_todo)
    _refresh_order(order)
    current["water_supply"] = water
    return "redelivery_reserved", current, {
        "todo_id": todo_id,
        "order_id": order["order_id"],
        "line": new_line,
        "adjustment": adjustment,
        "available_after": round(available - allocated, 6),
    }


def complete_todo(current, payload, actor):
    water = _water(current)
    todo_id = _text(payload, "todo_id")
    todo = next((item for item in water.setdefault("todos", []) if item.get("todo_id") == todo_id), None)
    if todo is None:
        raise DomainError("todo_not_found", "补送待办不存在", 404)
    if todo.get("status") not in {"open", "scheduled"}:
        raise DomainError("todo_closed", "补送待办已关闭", 409)
    todo["status"] = "completed"
    todo["completed_at"] = _now()
    todo["completed_by"] = actor
    current["water_supply"] = water
    return "todo_completed", current, {"todo_id": todo_id, "todo": todo}


def _close_order_lines(order, water, terminal_status, reason, actor, record_type, create_todo=False, todo_kind=None):
    for line in order.get("lines", []):
        if line.get("status") in {"reserved", "partial", "partially_delivered"}:
            remaining = round(float(line["reserved_volume"]) - float(line["delivered_volume"]), 6)
            if remaining > 0:
                line["returned_volume"] = round(float(line.get("returned_volume", 0)) + remaining, 6)
                water.setdefault("returns", []).append(
                    _return_record(order["order_id"], line["zone_id"], order["source_id"], remaining, reason, actor, record_type)
                )
                if create_todo:
                    water.setdefault("todos", []).append(
                        _todo_record(order["order_id"], line["zone_id"], order["source_id"], remaining, reason, todo_kind)
                    )
            line["status"] = terminal_status


def cancel_dispatch(current, payload, actor):
    water = _water(current)
    order = _find_order(water, _text(payload, "order_id"))
    if order is None:
        raise DomainError("dispatch_order_not_found", "调度单不存在", 404)
    if not _active_order(order):
        raise DomainError("invalid_dispatch_state", "只能撤销尚未完成的调度单", 409)
    reason = _text(payload, "reason")
    _close_order_lines(order, water, "cancelled", reason, actor, "cancellation_return")
    for todo in water.setdefault("todos", []):
        if todo.get("order_id") == order["order_id"] and todo.get("status") in {"open", "scheduled"}:
            todo["status"] = "cancelled"
            todo["completed_at"] = _now()
            todo["completed_by"] = actor
    _refresh_order(order)
    current["water_supply"] = water
    return "dispatch_cancelled", current, {
        "order_id": order["order_id"],
        "reason": reason,
        "returned_volume": order["returned_volume"],
    }


def release_dispatch_for_contamination(current, contaminated, actor):
    water = _water(current)
    if contaminated:
        for order in water.get("orders", []):
            if not _active_order(order):
                continue
            _close_order_lines(
                order,
                water,
                "released",
                "retest_contamination",
                actor,
                "contamination_release",
            )
            _refresh_order(order)
    current["water_supply"] = water


def is_legacy_water_event(payload):
    return bool(payload.get("alternate_source_id")) and not payload.get("water_supply")


def water_view(payload, source_map=None):
    source_map = source_map or {}
    if is_legacy_water_event(payload):
        source_id = payload.get("alternate_source_id")
        source = source_map.get(source_id)
        return {
            "legacy": True,
            "alternate_source_id": source_id,
            "source": {
                "source_id": source_id,
                "name": source.get("name") if source else None,
                "capacity_volume": source.get("capacity_volume") if source else None,
            },
            "zones": [
                {"zone_id": zone_id, "status": "legacy_switch", "requested_volume": None,
                 "reserved_volume": None, "delivered_volume": None, "remaining_volume": None}
                for zone_id in payload.get("zone_ids", [])
            ],
            "remaining_volume": None,
            "returns": [],
            "todos": [],
            "notifications": payload.get("notifications", []),
        }

    water = _water(payload)
    by_zone = {}
    for zone_id in payload.get("zone_ids", []):
        by_zone[zone_id] = {
            "zone_id": zone_id,
            "status": "not_scheduled",
            "requested_volume": 0.0,
            "reserved_volume": 0.0,
            "delivered_volume": 0.0,
            "remaining_volume": 0.0,
        }
    remaining_volume = 0.0
    source_ids = set()
    for order in water.get("orders", []):
        source_ids.add(order.get("source_id"))
        for line in order.get("lines", []):
            if line.get("status") in {"reserved", "partial", "partially_delivered"}:
                remaining_volume += float(line.get("reserved_volume", 0)) - float(line.get("delivered_volume", 0))
        for line in order.get("lines", []):
            zone = by_zone.setdefault(line["zone_id"], {
                "zone_id": line["zone_id"],
                "status": "scheduled",
                "requested_volume": 0.0,
                "reserved_volume": 0.0,
                "delivered_volume": 0.0,
                "remaining_volume": 0.0,
            })
            zone["requested_volume"] = round(zone["requested_volume"] + line.get("requested_volume", 0), 6)
            zone["reserved_volume"] = round(zone["reserved_volume"] + line.get("reserved_volume", 0), 6)
            zone["delivered_volume"] = round(zone["delivered_volume"] + line.get("delivered_volume", 0), 6)
            if line.get("status") in {"reserved", "partial", "partially_delivered"}:
                zone["remaining_volume"] = round(
                    zone["remaining_volume"] + line.get("reserved_volume", 0) - line.get("delivered_volume", 0), 6
                )
                zone["status"] = line["status"]
            elif zone["status"] == "not_scheduled":
                zone["status"] = line["status"]
    return {
        "legacy": False,
        "orders": water.get("orders", []),
        "source_ids": sorted(source for source in source_ids if source),
        "zones": list(by_zone.values()),
        "remaining_volume": round(remaining_volume, 6),
        "returns": water.get("returns", []),
        "deliveries": water.get("deliveries", []),
        "todos": water.get("todos", []),
        "notifications": payload.get("notifications", []),
    }


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "verify":
        _need_status(item, {"detected", "verified"})
        sample_count = int(payload.get("sample_count", 0) or 0)
        if sample_count < 1:
            raise DomainError("sample_required", "需要至少一份复检样本", 409)
        current["assessment"] = assess(current)
        current["verification"] = {"sample_count": sample_count, "note": payload.get("note", "")}
        return "verified", current, {"assessment": current["assessment"], "verification": current["verification"]}

    if action == "advise":
        _need_status(item, {"verified", "advisory", "dispatch_reserved"})
        notice_id = _text(payload, "notice_id")
        notice = {
            "notice_id": notice_id,
            "kind": _text(payload, "kind"),
            "message": _text(payload, "message"),
        }
        notices = current.setdefault("notifications", [])
        if any(existing.get("notice_id") == notice_id for existing in notices):
            raise DomainError("duplicate_notification", "同一通知编号不能重复发送", 409)
        notices.append(notice)
        return "advisory", current, {"notice": notice}

    if action == "switch_source":
        _need_status(item, {"verified", "advisory", "flushing", "disinfected", "sampled", "switched", "dispatch_reserved", "dispatch_cancelled", "redelivery_reserved", "delivery_reported"})
        alternate = _text(payload, "alternate_source_id")
        current["alternate_source_id"] = alternate
        return "switched", current, {"alternate_source_id": alternate}

    if action == "submit_dispatch":
        _need_status(item, {"verified", "advisory", "flushing", "disinfected", "sampled", "dispatch_reserved", "switched"})
        return submit_dispatch(current, payload, payload.pop("_backup_source"), actor)

    if action == "report_delivery":
        return report_delivery(current, payload, actor, status)

    if action == "complete_todo":
        return complete_todo(current, payload, actor)

    if action == "cancel_dispatch":
        return cancel_dispatch(current, payload, actor)

    if action == "redeliver":
        return redeliver(current, payload, payload.pop("_backup_source"), actor)

    if action == "flush":
        _need_status(item, {"advisory", "flushing", "switched", "dispatch_reserved"})
        zone_id = _text(payload, "zone_id")
        current.setdefault("response_actions", []).append({"type": "flush", "zone_id": zone_id})
        return "flushing", current, {"zone_id": zone_id, "type": "flush"}

    if action == "disinfect":
        _need_status(item, {"flushing", "disinfected"})
        if not payload.get("completed"):
            raise DomainError("disinfection_incomplete", "消毒尚未完成", 409)
        zone_id = _text(payload, "zone_id")
        current.setdefault("response_actions", []).append({"type": "disinfect", "zone_id": zone_id})
        return "disinfected", current, {"zone_id": zone_id, "type": "disinfect"}

    if action == "sample":
        _need_status(item, {"disinfected", "sampled"})
        result = {
            "sample_id": _text(payload, "sample_id"),
            "zone_id": _text(payload, "zone_id"),
            "concentration": float(payload.get("concentration", 0)),
        }
        if result["concentration"] < 0:
            raise DomainError("invalid_concentration", "浓度不能为负数")
        current.setdefault("sample_results", []).append(result)
        contaminated = result["concentration"] > float(current.get("limit", 0))
        release_dispatch_for_contamination(current, contaminated, actor)
        event = {"sample_result": result}
        if contaminated:
            event["dispatch_released"] = True
        return "sampled", current, event

    if action == "restore":
        _need_status(item, {"sampled"})
        if not payload.get("all_zones_cleared"):
            raise DomainError("zones_not_cleared", "仍有区域未完成水质恢复", 409)
        limit = float(current.get("limit", 0))
        results = current.get("sample_results", [])
        if not results or any(float(result["concentration"]) > limit for result in results):
            raise DomainError("quality_not_met", "复检结果未全部达到限值", 409)
        current["restoration"] = {"actor": actor, "note": payload.get("note", "")}
        return "restored", current, {"restoration": current["restoration"]}

    if action == "cancel":
        _need_status(item, {"detected", "verified", "advisory", "flushing", "disinfected", "sampled", "switched", "dispatch_reserved", "redelivery_reserved", "dispatch_cancelled"})
        reason = _text(payload, "reason")
        water = _water(current)
        for order in water.get("orders", []):
            if _active_order(order):
                _close_order_lines(order, water, "cancelled", reason, actor, "event_cancellation_return")
                for todo in water.setdefault("todos", []):
                    if todo.get("order_id") == order["order_id"] and todo.get("status") in {"open", "scheduled"}:
                        todo["status"] = "cancelled"
                        todo["completed_at"] = _now()
                        todo["completed_by"] = actor
                _refresh_order(order)
        current["water_supply"] = water
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
