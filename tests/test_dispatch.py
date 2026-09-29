import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.repository import Repository
from src.service import Service
from src.domain import ConflictError, DomainError, NotFoundError


class DispatchTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.repo = Repository(self.tmp.name)
        self.repo.initialize()
        self.service = Service(self.repo)

    def tearDown(self):
        os.unlink(self.tmp.name)

    def make_item(self, source_id="SRC-1", zones=("Z-1", "Z-2")):
        return self.service.create_item({
            "source_id": source_id,
            "contaminant": "nitrate",
            "detected_at": "2026-09-27T06:00:00+00:00",
            "concentration": 20,
            "limit": 10,
            "zone_ids": list(zones),
            "population": 5000,
        }, "analyst-1", "analyst")

    def make_source(self, code="BK-1", capacity=100.0):
        return self.service.create_backup_source(
            {"source_code": code, "name": "备用水库", "capacity": capacity},
            "coord-1", "coordinator",
        )


class ReservationTest(DispatchTestBase):
    def test_requests_reserved_in_order_and_latecomers_adjusted(self):
        self.make_source(capacity=100.0)
        item_a = self.make_item("SRC-1")
        order_a = self.service.create_dispatch(item_a["id"], {
            "source_code": "BK-1",
            "zones": [{"zone_id": "Z-1", "amount": 60}, {"zone_id": "Z-2", "amount": 60}],
        }, "disp-1", "dispatcher")
        reserved = {r["zone_id"]: r["reserved"] for r in order_a["reservations"]}
        self.assertEqual(reserved, {"Z-1": 60.0, "Z-2": 40.0})
        self.assertEqual(order_a["status"], "reserved")

        item_b = self.make_item("SRC-2", ("Z-3",))
        order_b = self.service.create_dispatch(item_b["id"], {
            "source_code": "BK-1",
            "zones": [{"zone_id": "Z-3", "amount": 50}],
        }, "disp-1", "dispatcher")
        self.assertEqual(order_b["reservations"][0]["reserved"], 0.0)
        self.assertEqual(order_b["reservations"][0]["requested"], 50.0)

        sources = self.service.backup_sources()
        self.assertEqual(sources[0]["remaining"], 0.0)
        self.assertEqual(sources[0]["held"], 100.0)

    def test_duplicate_request_id_rejected(self):
        self.make_source()
        item = self.make_item()
        payload = {
            "source_code": "BK-1",
            "request_id": "REQ-1",
            "zones": [{"zone_id": "Z-1", "amount": 10}],
        }
        self.service.create_dispatch(item["id"], payload, "disp-1", "dispatcher")
        with self.assertRaises(ConflictError) as context:
            self.service.create_dispatch(item["id"], payload, "disp-1", "dispatcher")
        self.assertEqual(context.exception.code, "duplicate_dispatch")

    def test_dispatch_requires_open_item_and_role(self):
        self.make_source()
        item = self.make_item()
        with self.assertRaises(DomainError) as context:
            self.service.create_dispatch(item["id"], {
                "source_code": "BK-1", "zones": [{"zone_id": "Z-1", "amount": 10}],
            }, "lab-1", "lab")
        self.assertEqual(context.exception.status, 403)
        item = self.service.act(item["id"], "cancel", {"reason": "误报"}, "coord-1", "coordinator", item["version"])
        with self.assertRaises(DomainError) as context:
            self.service.create_dispatch(item["id"], {
                "source_code": "BK-1", "zones": [{"zone_id": "Z-1", "amount": 10}],
            }, "disp-1", "dispatcher")
        self.assertEqual(context.exception.code, "invalid_state")

    def test_unknown_backup_source_rejected(self):
        item = self.make_item()
        with self.assertRaises(NotFoundError):
            self.service.create_dispatch(item["id"], {
                "source_code": "BK-X", "zones": [{"zone_id": "Z-1", "amount": 10}],
            }, "disp-1", "dispatcher")


class DeliveryFailureTest(DispatchTestBase):
    def setUp(self):
        super().setUp()
        self.make_source(capacity=100.0)
        self.item = self.make_item()
        self.order = self.service.create_dispatch(self.item["id"], {
            "source_code": "BK-1",
            "zones": [{"zone_id": "Z-1", "amount": 60}, {"zone_id": "Z-2", "amount": 40}],
        }, "disp-1", "dispatcher")

    def test_failed_zone_refunds_reservation_and_creates_todo(self):
        order = self.service.dispatch_action(
            self.order["id"], "deliver", {"zone_id": "Z-1"}, "field-1", "field_operator"
        )
        order = self.service.dispatch_action(
            self.order["id"], "fail", {"zone_id": "Z-2", "delivered_amount": 10}, "field-1", "field_operator"
        )
        by_zone = {r["zone_id"]: r for r in order["reservations"]}
        self.assertEqual(by_zone["Z-1"]["status"], "delivered")
        self.assertEqual(by_zone["Z-1"]["delivered"], 60.0)
        self.assertEqual(by_zone["Z-2"]["status"], "failed")
        self.assertEqual(by_zone["Z-2"]["delivered"], 10.0)
        self.assertEqual(order["status"], "completed")

        todos = [t for t in order["todos"] if t["status"] == "open"]
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["zone_id"], "Z-2")
        self.assertEqual(todos[0]["amount"], 30.0)

        sources = self.service.backup_sources()
        self.assertEqual(sources[0]["remaining"], 30.0)

        kinds = [entry["kind"] for entry in self.service.repository.item_dispatch(self.item["id"])["ledger"]]
        self.assertIn("refund", kinds)
        self.assertIn("deliver", kinds)

    def test_redeliver_consumes_remaining_and_closes_todo(self):
        order = self.service.dispatch_action(
            self.order["id"], "fail", {"zone_id": "Z-2", "delivered_amount": 0}, "field-1", "field_operator"
        )
        todo = order["todos"][0]
        order = self.service.dispatch_action(
            self.order["id"], "redeliver", {"todo_id": todo["id"]}, "field-1", "field_operator"
        )
        by_zone = {r["zone_id"]: r for r in order["reservations"]}
        self.assertEqual(by_zone["Z-2"]["status"], "delivered")
        self.assertEqual(by_zone["Z-2"]["delivered"], 40.0)
        self.assertEqual(order["todos"][0]["status"], "done")
        self.assertEqual(self.service.backup_sources()[0]["remaining"], 0.0)
        with self.assertRaises(ConflictError) as context:
            self.service.dispatch_action(
                self.order["id"], "redeliver", {"todo_id": todo["id"]}, "field-1", "field_operator"
            )
        self.assertEqual(context.exception.code, "todo_closed")

    def test_redeliver_capped_by_remaining(self):
        self.service.dispatch_action(
            self.order["id"], "deliver", {"zone_id": "Z-1"}, "field-1", "field_operator"
        )
        order = self.service.dispatch_action(
            self.order["id"], "fail", {"zone_id": "Z-2", "delivered_amount": 0}, "field-1", "field_operator"
        )
        todo = order["todos"][0]
        other = self.make_item("SRC-9", ("Z-9",))
        self.service.create_dispatch(other["id"], {
            "source_code": "BK-1", "zones": [{"zone_id": "Z-9", "amount": 30}],
        }, "disp-1", "dispatcher")
        order = self.service.dispatch_action(
            self.order["id"], "redeliver", {"todo_id": todo["id"]}, "field-1", "field_operator"
        )
        by_zone = {r["zone_id"]: r for r in order["reservations"]}
        self.assertEqual(by_zone["Z-2"]["delivered"], 10.0)
        self.assertEqual(self.service.backup_sources()[0]["remaining"], 0.0)


class ReleaseTest(DispatchTestBase):
    def setUp(self):
        super().setUp()
        self.make_source(capacity=100.0)
        self.item = self.make_item()
        self.order = self.service.create_dispatch(self.item["id"], {
            "source_code": "BK-1",
            "zones": [{"zone_id": "Z-1", "amount": 60}, {"zone_id": "Z-2", "amount": 40}],
        }, "disp-1", "dispatcher")

    def test_recheck_contamination_releases_pending_keeps_delivered(self):
        self.service.dispatch_action(
            self.order["id"], "deliver", {"zone_id": "Z-1"}, "field-1", "field_operator"
        )
        order = self.service.dispatch_action(
            self.order["id"], "recheck", {"result": "contaminated", "note": "备用水源检出污染"},
            "lab-1", "lab",
        )
        self.assertEqual(order["status"], "released")
        by_zone = {r["zone_id"]: r for r in order["reservations"]}
        self.assertEqual(by_zone["Z-1"]["status"], "delivered")
        self.assertEqual(by_zone["Z-1"]["delivered"], 60.0)
        self.assertEqual(by_zone["Z-2"]["status"], "released")
        self.assertEqual(self.service.backup_sources()[0]["remaining"], 40.0)
        ledger = self.service.repository.item_dispatch(self.item["id"])["ledger"]
        self.assertIn("release", [entry["kind"] for entry in ledger])
        with self.assertRaises(ConflictError):
            self.service.dispatch_action(
                self.order["id"], "deliver", {"zone_id": "Z-2"}, "field-1", "field_operator"
            )

    def test_recheck_clear_keeps_reservations(self):
        order = self.service.dispatch_action(
            self.order["id"], "recheck", {"result": "clear"}, "lab-1", "lab"
        )
        self.assertEqual(order["status"], "reserved")
        self.assertEqual(self.service.backup_sources()[0]["remaining"], 0.0)

    def test_revoke_releases_pending_and_cancels_todos(self):
        order = self.service.dispatch_action(
            self.order["id"], "fail", {"zone_id": "Z-2", "delivered_amount": 5}, "field-1", "field_operator"
        )
        order = self.service.dispatch_action(
            self.order["id"], "revoke", {"reason": "改用其他水源"}, "coord-1", "coordinator"
        )
        self.assertEqual(order["status"], "revoked")
        by_zone = {r["zone_id"]: r for r in order["reservations"]}
        self.assertEqual(by_zone["Z-1"]["status"], "released")
        self.assertEqual(by_zone["Z-2"]["status"], "failed")
        self.assertEqual(by_zone["Z-2"]["delivered"], 5.0)
        self.assertTrue(all(t["status"] == "cancelled" for t in order["todos"]))
        self.assertEqual(self.service.backup_sources()[0]["remaining"], 95.0)


class LegacyAndSummaryTest(DispatchTestBase):
    def test_legacy_single_source_event_still_viewable(self):
        item = self.make_item()
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        item = self.service.act(
            item["id"], "switch_source", {"alternate_source_id": "ALT-OLD"}, "coord-1", "coordinator", item["version"]
        )
        view = self.service.get_item(item["id"])
        self.assertEqual(view["legacy_alternate_source_id"], "ALT-OLD")
        self.assertEqual(view["payload"]["alternate_source_id"], "ALT-OLD")
        self.assertEqual(view["dispatch"], {"orders": [], "ledger": []})

    def test_summary_covers_sources_events_and_ledger(self):
        self.make_source(capacity=80.0)
        item = self.make_item()
        item = self.service.act(item["id"], "verify", {"sample_count": 1}, "analyst-1", "analyst", item["version"])
        item = self.service.act(
            item["id"], "advise",
            {"notice_id": "N-1", "kind": "boil", "message": "煮沸后饮用"},
            "disp-1", "dispatcher", item["version"],
        )
        order = self.service.create_dispatch(item["id"], {
            "source_code": "BK-1",
            "zones": [{"zone_id": "Z-1", "amount": 50}, {"zone_id": "Z-2", "amount": 50}],
        }, "disp-1", "dispatcher")
        self.service.dispatch_action(
            order["id"], "fail", {"zone_id": "Z-2", "delivered_amount": 0}, "field-1", "field_operator"
        )
        summary = self.service.dispatch_summary()
        self.assertEqual(summary["sources"][0]["remaining"], 30.0)
        event = summary["events"][0]
        self.assertEqual(event["item_id"], item["id"])
        self.assertEqual(len(event["notifications"]), 1)
        coverage = {row["zone_id"]: row for row in event["coverage"]}
        self.assertEqual(coverage["Z-1"]["reserved"], 50.0)
        self.assertEqual(coverage["Z-2"]["reserved"], 30.0)
        self.assertEqual(coverage["Z-2"]["status"], "failed")
        self.assertEqual(len(event["todos"]), 1)
        kinds = [entry["kind"] for entry in event["ledger"]]
        self.assertEqual(kinds.count("reserve"), 2)
        self.assertIn("refund", kinds)


if __name__ == "__main__":
    unittest.main()
