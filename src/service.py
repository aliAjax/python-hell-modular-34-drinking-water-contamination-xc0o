from . import dispatch, domain, rules
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
        item["dispatch"] = self.repository.item_dispatch(item_id)
        item["legacy_alternate_source_id"] = item["payload"].get("alternate_source_id")
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()

    def _require_identity(self, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)

    def create_backup_source(self, payload, actor, role):
        self._require_identity(actor, role)
        if role not in dispatch.BACKUP_SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能登记备用水源", 403)
        normalized = dispatch.normalize_backup_source(payload)
        return self.repository.create_backup_source(
            normalized["source_code"], normalized["name"], normalized["capacity"], actor, role
        )

    def backup_sources(self):
        return self.repository.list_backup_sources()

    def create_dispatch(self, item_id, payload, actor, role):
        self._require_identity(actor, role)
        if role not in dispatch.DISPATCH_CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交调度单", 403)
        item = self.repository.get_item(item_id)
        if item["status"] in dispatch.ITEM_CLOSED_STATUS:
            raise DomainError("invalid_state", "事件已关闭，不能申请备用水源调度", 409)
        normalized = dispatch.normalize_order(payload)
        return self.repository.create_dispatch_order(
            item_id,
            normalized["source_code"],
            normalized["requests"],
            normalized["request_id"],
            normalized["note"],
            actor,
            role,
        )

    def dispatch_action(self, order_id, action, payload, actor, role, expected_version=None):
        self._require_identity(actor, role)
        if action not in dispatch.DISPATCH_ACTION_ROLES:
            raise DomainError("unknown_action", "不支持的调度操作")
        if role not in dispatch.DISPATCH_ACTION_ROLES[action]:
            raise DomainError("forbidden", "当前角色不能执行该调度操作", 403)
        return self.repository.apply_dispatch_action(order_id, action, actor, role, payload, expected_version)

    def dispatch_summary(self):
        return self.repository.dispatch_summary()
