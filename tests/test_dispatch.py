import os
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import DomainError


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def create_event(self, source_id="SRC-X", zones=("Z-A", "Z-B"), detected_at=None):
        suffix = uuid.uuid4().hex[:8]
        return self.service.create_item({
            "source_id": source_id,
            "contaminant": "nitrate",
            "detected_at": detected_at or "2026-09-27T08:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": list(zones),
            "population": 1000,
        }, "analyst-1", "analyst")

    def register_source(self, source_id, capacity):
        return self.service.register_backup_source({
            "source_id": source_id,
            "name": "应急水车",
            "capacity_volume": capacity,
        }, "coord-1", "coordinator")

    def test_reserves_in_request_order_and_modifies_later_order(self):
        source_id = "BAK-%s" % uuid.uuid4().hex[:8]
        self.register_source(source_id, 100)
        first = self.create_event(source_id="P-1", zones=("Z-1", "Z-2"), detected_at="2026-09-27T08:00:01+00:00")
        second = self.create_event(source_id="P-2", zones=("Z-3", "Z-4"), detected_at="2026-09-27T08:00:02+00:00")
        first = self.service.act(first["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", first["version"])
        second = self.service.act(second["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", second["version"])

        first = self.service.act(first["id"], "submit_dispatch", {
            "source_id": source_id,
            "zones": [
                {"zone_id": "Z-1", "requested_volume": 60},
                {"zone_id": "Z-2", "requested_volume": 20},
            ],
        }, "disp-1", "dispatcher", first["version"])
        second = self.service.act(second["id"], "submit_dispatch", {
            "source_id": source_id,
            "zones": [
                {"zone_id": "Z-3", "requested_volume": 30},
                {"zone_id": "Z-4", "requested_volume": 20},
            ],
        }, "disp-1", "dispatcher", second["version"])

        first_order = first["payload"]["water_supply"]["orders"][0]
        second_order = second["payload"]["water_supply"]["orders"][0]
        self.assertEqual(first_order["reserved_volume"], 80)
        self.assertEqual(second_order["reserved_volume"], 20)
        self.assertEqual(second_order["lines"][0]["reserved_volume"], 20)
        self.assertEqual(second_order["lines"][1]["reserved_volume"], 0)
        self.assertEqual(second_order["adjustments"][0]["shortage_volume"], 10)
        self.assertEqual(second_order["adjustments"][1]["shortage_volume"], 20)
        sources = {item["source_id"]: item for item in self.service.list_backup_sources()}
        self.assertEqual(sources[source_id]["used_volume"], 100)
        self.assertEqual(sources[source_id]["available_volume"], 0)

    def test_partial_delivery_failure_returns_remaining_and_creates_redelivery_todo(self):
        source_id = "BAK-%s" % uuid.uuid4().hex[:8]
        self.register_source(source_id, 50)
        item = self.create_event(zones=("Z-A", "Z-B"), detected_at="2026-09-27T09:00:01+00:00")
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "submit_dispatch", {
            "source_id": source_id,
            "zones": [
                {"zone_id": "Z-A", "requested_volume": 30},
                {"zone_id": "Z-B", "requested_volume": 20},
            ],
        }, "coord-1", "coordinator", item["version"])
        order_id = item["payload"]["water_supply"]["orders"][0]["order_id"]

        item = self.service.act(item["id"], "report_delivery", {
            "order_id": order_id,
            "zone_id": "Z-A",
            "delivered_volume": 20,
        }, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "report_delivery", {
            "order_id": order_id,
            "zone_id": "Z-A",
            "delivered_volume": 0,
            "failed": True,
            "reason": "road_closed",
        }, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "report_delivery", {
            "order_id": order_id,
            "zone_id": "Z-B",
            "delivered_volume": 20,
        }, "field-1", "field_operator", item["version"])

        order = item["payload"]["water_supply"]["orders"][0]
        self.assertEqual(order["status"], "failed")
        failed_line = next(line for line in order["lines"] if line["zone_id"] == "Z-A")
        self.assertEqual(failed_line["delivered_volume"], 20)
        self.assertEqual(failed_line["returned_volume"], 10)
        self.assertEqual(item["water"]["returns"][0]["volume"], 10)
        todo = item["water"]["todos"][0]
        self.assertEqual(todo["volume"], 10)
        self.assertEqual(item["water"]["remaining_volume"], 0)
        self.assertEqual(self.service.list_backup_sources()[0]["available_volume"], 10)

        item = self.service.act(item["id"], "redeliver", {
            "todo_id": todo["todo_id"],
        }, "coord-1", "coordinator", item["version"])
        order = item["payload"]["water_supply"]["orders"][0]
        self.assertEqual(order["status"], "reserved")
        redelivery_line = next(line for line in order["lines"] if line.get("redelivery_for_todo_id"))
        self.assertEqual(redelivery_line["reserved_volume"], 10)
        item = self.service.act(item["id"], "report_delivery", {
            "order_id": order_id,
            "zone_id": "Z-A",
            "delivered_volume": 10,
        }, "field-1", "field_operator", item["version"])
        order = item["payload"]["water_supply"]["orders"][0]
        self.assertEqual(order["status"], "completed")
        completed_todo = item["water"]["todos"][0]
        self.assertEqual(completed_todo["status"], "completed")

    def test_cancelled_dispatch_releases_unexecuted_but_keeps_delivered(self):
        source_id = "BAK-%s" % uuid.uuid4().hex[:8]
        self.register_source(source_id, 40)
        item = self.create_event(zones=("Z-C",), detected_at="2026-09-27T10:00:01+00:00")
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "submit_dispatch", {
            "source_id": source_id,
            "zones": [{"zone_id": "Z-C", "requested_volume": 40}],
        }, "coord-1", "coordinator", item["version"])
        order_id = item["payload"]["water_supply"]["orders"][0]["order_id"]
        item = self.service.act(item["id"], "report_delivery", {
            "order_id": order_id,
            "zone_id": "Z-C",
            "delivered_volume": 10,
        }, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "cancel_dispatch", {
            "order_id": order_id,
            "reason": "schedule_revoked",
        }, "coord-1", "coordinator", item["version"])

        order = item["payload"]["water_supply"]["orders"][0]
        self.assertEqual(order["status"], "cancelled")
        self.assertEqual(order["delivered_volume"], 10)
        self.assertEqual(item["water"]["returns"][0]["volume"], 30)
        self.assertEqual(self.service.list_backup_sources()[0]["available_volume"], 30)

    def test_persistent_retest_releases_unexecuted_reservations_and_keeps_delivery(self):
        source_id = "BAK-%s" % uuid.uuid4().hex[:8]
        self.register_source(source_id, 50)
        item = self.create_event(zones=("Z-D", "Z-E"), detected_at="2026-09-27T11:00:01+00:00")
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "submit_dispatch", {
            "source_id": source_id,
            "zones": [
                {"zone_id": "Z-D", "requested_volume": 20},
                {"zone_id": "Z-E", "requested_volume": 30},
            ],
        }, "coord-1", "coordinator", item["version"])
        order_id = item["payload"]["water_supply"]["orders"][0]["order_id"]
        item = self.service.act(item["id"], "report_delivery", {
            "order_id": order_id,
            "zone_id": "Z-D",
            "delivered_volume": 20,
        }, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "flush", {"zone_id": "Z-D"}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "disinfect", {"zone_id": "Z-D", "completed": True}, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "sample", {
            "sample_id": "LAB-1",
            "zone_id": "Z-D",
            "concentration": 15,
        }, "lab-1", "lab", item["version"])

        order = item["payload"]["water_supply"]["orders"][0]
        self.assertEqual(order["status"], "released")
        self.assertEqual(order["delivered_volume"], 20)
        self.assertEqual(item["water"]["returns"][0]["volume"], 30)
        self.assertEqual(self.service.list_backup_sources()[0]["available_volume"], 30)

    def test_event_cancel_releases_dispatch_and_keeps_delivery(self):
        source_id = "BAK-%s" % uuid.uuid4().hex[:8]
        self.register_source(source_id, 30)
        item = self.create_event(zones=("Z-X",), detected_at="2026-09-27T13:00:01+00:00")
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "submit_dispatch", {
            "source_id": source_id,
            "zones": [{"zone_id": "Z-X", "requested_volume": 30}],
        }, "coord-1", "coordinator", item["version"])
        order_id = item["payload"]["water_supply"]["orders"][0]["order_id"]
        item = self.service.act(item["id"], "report_delivery", {
            "order_id": order_id,
            "zone_id": "Z-X",
            "delivered_volume": 8,
        }, "field-1", "field_operator", item["version"])
        item = self.service.act(item["id"], "cancel", {"reason": "false_alarm"}, "coord-1", "coordinator", item["version"])
        order = item["payload"]["water_supply"]["orders"][0]
        self.assertEqual(item["status"], "cancelled")
        self.assertEqual(order["delivered_volume"], 8)
        self.assertEqual(item["water"]["returns"][0]["volume"], 22)
        self.assertEqual(self.service.list_backup_sources()[0]["available_volume"], 22)

    def test_legacy_single_source_field_remains_viewable(self):
        item = self.create_event(zones=("Z-OLD",), detected_at="2026-09-27T12:00:01+00:00")
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        item = self.service.act(item["id"], "switch_source", {"alternate_source_id": "OLD-ALT"}, "coord-1", "coordinator", item["version"])
        self.assertTrue(item["water"]["legacy"])
        self.assertEqual(item["water"]["alternate_source_id"], "OLD-ALT")
        self.assertEqual(item["water"]["zones"][0]["zone_id"], "Z-OLD")


if __name__ == "__main__":
    unittest.main()
