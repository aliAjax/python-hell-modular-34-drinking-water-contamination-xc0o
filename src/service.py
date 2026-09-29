from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def register_backup_source(self, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.BACKUP_SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能登记备用水源", 403)
        normalized = domain.normalize_backup_source(payload)
        return self.repository.register_backup_source(
            normalized["source_id"], normalized["name"], normalized["capacity_volume"], actor, role
        )

    def list_backup_sources(self):
        sources = self.repository.list_backup_sources()
        used = {source["source_id"]: 0.0 for source in sources}
        for item in self.repository.list_items():
            water = item["payload"].get("water_supply") or {}
            for order in water.get("orders", []):
                source_id = order.get("source_id")
                if source_id not in used:
                    continue
                for line in order.get("lines", []):
                    used[source_id] += float(line.get("delivered_volume", 0))
                    if line.get("status") in {"reserved", "partial", "partially_delivered"}:
                        used[source_id] += float(line.get("reserved_volume", 0)) - float(line.get("delivered_volume", 0))
        result = []
        for source in sources:
            value = dict(source)
            value["used_volume"] = round(used.get(source["source_id"], 0.0), 6)
            value["available_volume"] = round(float(source["capacity_volume"]) - value["used_volume"], 6)
            result.append(value)
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        if action == "submit_dispatch":
            normalized = domain.normalize_dispatch(payload)
            return self.repository.source_dispatch_action(item_id, action, normalized, actor, role, expected_version)
        if action == "redeliver":
            if not isinstance(payload, dict):
                payload = {}
            return self.repository.source_dispatch_action(item_id, action, payload, actor, role, expected_version)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        source_map = {source["source_id"]: source for source in self.repository.list_backup_sources()}
        item["water"] = rules.water_view(item["payload"], source_map)
        return item

    def list_items(self, status=None):
        return [self.get_item(item["id"]) for item in self.repository.list_items(status)]

    def state(self):
        counts = self.repository.state_summary()["counts"]
        return {
            "counts": counts,
            "items": self.list_items(),
            "backup_sources": self.list_backup_sources(),
        }
