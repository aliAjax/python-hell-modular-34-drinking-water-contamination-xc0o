from datetime import datetime


class DomainError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.status = status


class ConflictError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 409)


class NotFoundError(DomainError):
    def __init__(self, code, message):
        super().__init__(code, message, 404)


def require_text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def number(payload, name, minimum=None):
    value = payload.get(name)
    if isinstance(value, bool):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise DomainError("invalid_number", "%s 必须是数字" % name)
    if minimum is not None and value < minimum:
        raise DomainError("invalid_number", "%s 不能小于 %s" % (name, minimum))
    return value


def parse_timestamp(payload, name):
    value = require_text(payload, name)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainError("invalid_timestamp", "%s 必须是 ISO 时间" % name)
    return value


def normalize_create(payload):
    source_id = require_text(payload, "source_id")
    contaminant = require_text(payload, "contaminant")
    detected_at = parse_timestamp(payload, "detected_at")
    concentration = number(payload, "concentration", 0)
    limit = number(payload, "limit", 0.000001)
    zones = payload.get("zone_ids", [])
    if not isinstance(zones, list) or not zones:
        raise DomainError("zones_required", "至少需要一个受影响区域")
    if any(not isinstance(zone, str) or not zone.strip() for zone in zones):
        raise DomainError("invalid_zones", "区域编号必须是字符串列表")
    population = int(payload.get("population", 0) or 0)
    if population < 0:
        raise DomainError("invalid_population", "受影响人数不能为负数")
    stable_key = "%s|%s|%s" % (source_id, contaminant, detected_at)
    return {
        "source_id": source_id,
        "contaminant": contaminant,
        "detected_at": detected_at,
        "concentration": concentration,
        "limit": limit,
        "zone_ids": [zone.strip() for zone in zones],
        "population": population,
        "complaints": int(payload.get("complaints", 0) or 0),
        "notifications": [],
        "response_actions": [],
        "sample_results": [],
        "_stable_key": stable_key,
    }


def normalize_source(payload):
    source_type = require_text(payload, "source_type")
    external_id = require_text(payload, "external_id")
    observed_at = parse_timestamp(payload, "observed_at")
    result = {
        "source_type": source_type,
        "external_id": external_id,
        "observed_at": observed_at,
        "concentration": number(payload, "concentration", 0) if "concentration" in payload else None,
        "zone_id": payload.get("zone_id"),
        "note": payload.get("note", ""),
    }
    return result


def normalize_backup_source(payload):
    source_id = require_text(payload, "source_id")
    name = payload.get("name", "")
    if not isinstance(name, str):
        raise DomainError("invalid_name", "备用水源名称必须是字符串")
    return {
        "source_id": source_id,
        "name": name.strip(),
        "capacity_volume": number(payload, "capacity_volume", 0.000001),
    }


def normalize_dispatch(payload):
    source_id = require_text(payload, "source_id")
    raw_zones = payload.get("zones")
    if not isinstance(raw_zones, list) or not raw_zones:
        raise DomainError("zones_required", "调度单至少需要一个送水片区")
    zones = []
    seen = set()
    for raw in raw_zones:
        if not isinstance(raw, dict):
            raise DomainError("invalid_zone", "片区申请必须是对象列表")
        zone_id = require_text(raw, "zone_id")
        if zone_id in seen:
            raise DomainError("duplicate_zone", "同一调度单不能重复申请同一片区")
        seen.add(zone_id)
        zones.append({
            "zone_id": zone_id,
            "requested_volume": number(raw, "requested_volume", 0.000001),
        })
    order_id = payload.get("order_id", "")
    if order_id is not None and not isinstance(order_id, str):
        raise DomainError("invalid_order_id", "调度单编号必须是字符串")
    order_id = order_id.strip() if order_id else ""
    return {"source_id": source_id, "order_id": order_id, "zones": zones}
