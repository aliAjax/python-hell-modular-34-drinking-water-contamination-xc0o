import json
import sqlite3
from datetime import datetime, timezone

from . import dispatch
from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backup_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    capacity REAL NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispatch_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_code TEXT NOT NULL,
                    request_id TEXT,
                    note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_dispatch_request
                    ON dispatch_orders(item_id, request_id) WHERE request_id IS NOT NULL;
                CREATE TABLE IF NOT EXISTS dispatch_reservations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    zone_id TEXT NOT NULL,
                    requested REAL NOT NULL,
                    reserved REAL NOT NULL,
                    delivered REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(order_id, zone_id),
                    FOREIGN KEY(order_id) REFERENCES dispatch_orders(id)
                );
                CREATE TABLE IF NOT EXISTS dispatch_todos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    zone_id TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    done_at TEXT,
                    FOREIGN KEY(order_id) REFERENCES dispatch_orders(id)
                );
                CREATE TABLE IF NOT EXISTS dispatch_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    zone_id TEXT,
                    amount REAL NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    actor TEXT,
                    role TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES dispatch_orders(id)
                );
                """
            )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()

    # ---- 备用水源与调度 ----

    def create_backup_source(self, source_code, name, capacity, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO backup_sources(source_code,name,capacity,created_by,created_role,created_at) VALUES(?,?,?,?,?,?)",
                    (source_code, name, capacity, actor, role, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source_code", "备用水源编号已存在")
            self.append_audit(
                conn, None, "backup_source_created", actor, role,
                {"source_code": source_code, "name": name, "capacity": capacity},
            )
            conn.execute("COMMIT")
            return {"source_code": source_code, "name": name, "capacity": capacity,
                    "held": 0.0, "remaining": capacity}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _source_hold(self, conn, source_code):
        row = conn.execute(
            """
            SELECT COALESCE(SUM(CASE WHEN r.status='pending' THEN r.reserved ELSE r.delivered END), 0) AS held
            FROM dispatch_reservations r
            JOIN dispatch_orders o ON r.order_id = o.id
            WHERE o.source_code=?
            """,
            (source_code,),
        ).fetchone()
        return round(float(row["held"]), 3)

    def list_backup_sources(self):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM backup_sources ORDER BY id").fetchall()
            result = []
            for row in rows:
                held = self._source_hold(conn, row["source_code"])
                result.append({
                    "source_code": row["source_code"],
                    "name": row["name"],
                    "capacity": row["capacity"],
                    "held": held,
                    "remaining": round(row["capacity"] - held, 3),
                })
            return result
        finally:
            conn.close()

    def _reservation_rows(self, conn, order_id):
        rows = conn.execute(
            "SELECT * FROM dispatch_reservations WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def _todo_rows(self, conn, order_id):
        rows = conn.execute(
            "SELECT * FROM dispatch_todos WHERE order_id=? ORDER BY id", (order_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_order(self, order_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM dispatch_orders WHERE id=?", (order_id,)).fetchone()
            if row is None:
                raise NotFoundError("dispatch_not_found", "调度单不存在")
            order = dict(row)
            order["reservations"] = self._reservation_rows(conn, order_id)
            order["todos"] = self._todo_rows(conn, order_id)
            return order
        finally:
            conn.close()

    def create_dispatch_order(self, item_id, source_code, requests, request_id, note, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id, status FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            source = conn.execute(
                "SELECT * FROM backup_sources WHERE source_code=?", (source_code,)
            ).fetchone()
            if source is None:
                raise NotFoundError("source_not_found", "备用水源不存在")
            if request_id is not None:
                existing = conn.execute(
                    "SELECT id FROM dispatch_orders WHERE item_id=? AND request_id=?",
                    (item_id, request_id),
                ).fetchone()
                if existing is not None:
                    raise ConflictError("duplicate_dispatch", "相同 request_id 的调度单已提交")
            remaining = round(float(source["capacity"]) - self._source_hold(conn, source_code), 3)
            adjusted = dispatch.adjust_requests(requests, remaining)
            now = now_iso()
            conn.execute(
                """INSERT INTO dispatch_orders(item_id,source_code,request_id,note,status,version,created_by,created_role,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item_id, source_code, request_id, note, "reserved", 1, actor, role, now, now),
            )
            order_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            ledger_rows = []
            for entry in adjusted:
                conn.execute(
                    """INSERT INTO dispatch_reservations(order_id,zone_id,requested,reserved,delivered,status,created_at,updated_at)
                       VALUES(?,?,?,?,0,'pending',?,?)""",
                    (order_id, entry["zone_id"], entry["requested"], entry["reserved"], now, now),
                )
                if entry["reserved"] < entry["requested"]:
                    detail = "按剩余水量改单：请求 %s，预留 %s" % (entry["requested"], entry["reserved"])
                else:
                    detail = "足额预留"
                ledger_rows.append((order_id, item_id, "reserve", entry["zone_id"], entry["reserved"], detail, actor, role, now))
            conn.executemany(
                """INSERT INTO dispatch_ledger(order_id,item_id,kind,zone_id,amount,detail,actor,role,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                ledger_rows,
            )
            self.append_audit(
                conn, item_id, "dispatch_created", actor, role,
                {"order_id": order_id, "source_code": source_code, "request_id": request_id, "reservations": adjusted},
            )
            conn.execute("COMMIT")
            return self.get_order(order_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def apply_dispatch_action(self, order_id, action, actor, role, payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM dispatch_orders WHERE id=?", (order_id,)).fetchone()
            if row is None:
                raise NotFoundError("dispatch_not_found", "调度单不存在")
            order = dict(row)
            if expected_version is not None and int(expected_version) != int(order["version"]):
                raise ConflictError("version_conflict", "调度单已被其他操作更新，请重新读取")
            reservations = self._reservation_rows(conn, order_id)
            todos = self._todo_rows(conn, order_id)
            source = conn.execute(
                "SELECT * FROM backup_sources WHERE source_code=?", (order["source_code"],)
            ).fetchone()
            remaining = round(float(source["capacity"]) - self._source_hold(conn, order["source_code"]), 3)
            plan = dispatch.plan_action(action, order, reservations, todos, payload, remaining)
            now = now_iso()
            for update in plan["reservations"]:
                conn.execute(
                    "UPDATE dispatch_reservations SET status=?, delivered=?, updated_at=? WHERE id=?",
                    (update["status"], update["delivered"], now, update["id"]),
                )
            for todo in plan["new_todos"]:
                conn.execute(
                    "INSERT INTO dispatch_todos(order_id,zone_id,amount,status,created_at) VALUES(?,?,?,'open',?)",
                    (order_id, todo["zone_id"], todo["amount"], now),
                )
            for todo_id in plan["done_todos"]:
                conn.execute(
                    "UPDATE dispatch_todos SET status='done', done_at=? WHERE id=?", (now, todo_id)
                )
            if plan["cancel_open_todos"]:
                conn.execute(
                    "UPDATE dispatch_todos SET status='cancelled', done_at=? WHERE order_id=? AND status='open'",
                    (now, order_id),
                )
            conn.execute(
                "UPDATE dispatch_orders SET status=?, version=?, updated_at=? WHERE id=?",
                (plan["status"], int(order["version"]) + 1, now, order_id),
            )
            ledger_rows = [
                (order_id, order["item_id"], entry["kind"], entry.get("zone_id"), entry["amount"],
                 entry.get("detail", ""), actor, role, now)
                for entry in plan["ledger"]
            ]
            if ledger_rows:
                conn.executemany(
                    """INSERT INTO dispatch_ledger(order_id,item_id,kind,zone_id,amount,detail,actor,role,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    ledger_rows,
                )
            event_payload = dict(plan["event"])
            event_payload["order_id"] = order_id
            self.append_audit(conn, order["item_id"], "dispatch_" + action, actor, role, event_payload)
            conn.execute("COMMIT")
            return self.get_order(order_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def item_dispatch(self, item_id):
        conn = self.connect()
        try:
            orders = []
            rows = conn.execute(
                "SELECT * FROM dispatch_orders WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
            for row in rows:
                order = dict(row)
                order["reservations"] = self._reservation_rows(conn, order["id"])
                order["todos"] = self._todo_rows(conn, order["id"])
                orders.append(order)
            ledger = conn.execute(
                "SELECT * FROM dispatch_ledger WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
            return {"orders": orders, "ledger": [dict(row) for row in ledger]}
        finally:
            conn.close()

    def dispatch_summary(self):
        conn = self.connect()
        try:
            sources = self.list_backup_sources()
            items = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            events = []
            for row in items:
                item = self._row_to_item(row)
                info = self.item_dispatch(item["id"])
                coverage = []
                todos = []
                for order in info["orders"]:
                    for reservation in order["reservations"]:
                        coverage.append({
                            "order_id": order["id"],
                            "source_code": order["source_code"],
                            "zone_id": reservation["zone_id"],
                            "requested": reservation["requested"],
                            "reserved": reservation["reserved"],
                            "delivered": reservation["delivered"],
                            "status": reservation["status"],
                        })
                    for todo in order["todos"]:
                        todos.append({
                            "id": todo["id"],
                            "order_id": order["id"],
                            "zone_id": todo["zone_id"],
                            "amount": todo["amount"],
                            "status": todo["status"],
                        })
                events.append({
                    "item_id": item["id"],
                    "status": item["status"],
                    "source_id": item["payload"].get("source_id"),
                    "contaminant": item["payload"].get("contaminant"),
                    "zone_ids": item["payload"].get("zone_ids", []),
                    "notifications": item["payload"].get("notifications", []),
                    "legacy_alternate_source_id": item["payload"].get("alternate_source_id"),
                    "coverage": coverage,
                    "todos": todos,
                    "ledger": info["ledger"],
                })
            return {"sources": sources, "events": events}
        finally:
            conn.close()
