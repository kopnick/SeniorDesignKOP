import importlib
import sys
import unittest
from unittest.mock import patch


class FakeDatabase:
    def __init__(self):
        self.rows = []
        self.events_exists = False
        self.events_partitioned = False
        self.events_legacy_exists = False
        self.partitions = set()


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._results = []
        self._result = None

    def execute(self, query, params=None):
        normalized = " ".join(query.split())
        self.rowcount = 0
        self._results = []
        self._result = None

        if "FROM pg_class c" in normalized and "c.relname = 'events'" in normalized:
            if self.db.events_exists:
                relkind = "p" if self.db.events_partitioned else "r"
                self._result = {"relkind": relkind, "partstrat": "r" if relkind == "p" else None}
            else:
                self._result = None
            return

        if normalized.startswith("ALTER TABLE events RENAME TO events_legacy"):
            self.db.events_exists = False
            self.db.events_legacy_exists = True
            return

        if normalized.startswith("CREATE TABLE IF NOT EXISTS events ("):
            self.db.events_exists = True
            self.db.events_partitioned = "PARTITION BY RANGE (event_date)" in normalized
            return

        if "WHERE table_name = 'events_legacy'" in normalized:
            self._result = {"exists": self.db.events_legacy_exists}
            return

        if normalized.startswith("SELECT DISTINCT TO_DATE(SPLIT_PART(timestamp, ' ', 1), 'YYYY-MM-DD') AS event_date FROM events_legacy"):
            self._results = []
            return

        if normalized.startswith("INSERT INTO events (device_id, sensor_name, state, timestamp, event_date, message_guid) SELECT"):
            return

        if normalized.startswith("DROP TABLE events_legacy"):
            self.db.events_legacy_exists = False
            return

        if normalized.startswith("CREATE TABLE IF NOT EXISTS events_p_") and "PARTITION OF events" in normalized:
            partition_name = normalized.split()[5]
            self.db.partitions.add(partition_name)
            return

        if "FROM pg_inherits i" in normalized and "p.relname = 'events'" in normalized:
            self._results = [{"partition_name": name} for name in sorted(self.db.partitions)]
            return

        if normalized.startswith("DROP TABLE IF EXISTS events_p_"):
            partition_name = normalized.split()[4].rstrip(";")
            self.db.partitions.discard(partition_name)
            return

        if normalized.startswith("INSERT INTO events"):
            device_id, sensor_name, state, timestamp, event_date, message_guid = params

            if "ON CONFLICT DO NOTHING" in normalized:
                if any(row["message_guid"] == message_guid for row in self.db.rows):
                    self.rowcount = 0
                    return

                if any(
                    row["device_id"] == int(device_id)
                    and row["sensor_name"] == sensor_name
                    and row["timestamp"] == timestamp
                    and row["event_date"] == event_date
                    for row in self.db.rows
                ):
                    self.rowcount = 0
                    return

            self.db.rows.append(
                {
                    "device_id": int(device_id),
                    "sensor_name": sensor_name,
                    "state": state,
                    "timestamp": timestamp,
                    "event_date": event_date,
                    "message_guid": message_guid,
                }
            )
            self.rowcount = 1
            return

        if normalized.startswith("SELECT device_id, sensor_name, state, timestamp, message_guid FROM events ORDER BY timestamp DESC, id DESC LIMIT 50;"):
            self._results = sorted(self.db.rows, key=lambda row: row["timestamp"], reverse=True)[:50]
            return

    def fetchall(self):
        return self._results

    def fetchone(self):
        return self._result

    def close(self):
        return None


class FakeConnection:
    def __init__(self, db):
        self.db = db

    def cursor(self):
        return FakeCursor(self.db)

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def load_processing_module(fake_db):
    def fake_connect(*args, **kwargs):
        return FakeConnection(fake_db)

    if "processing" in sys.modules:
        del sys.modules["processing"]

    with patch("psycopg2.connect", side_effect=fake_connect):
        return importlib.import_module("processing")


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.fake_db = FakeDatabase()
        self.processing = load_processing_module(self.fake_db)
        self.get_db_connection_patcher = patch.object(
            self.processing, "get_db_connection", return_value=FakeConnection(self.fake_db)
        )
        self.get_db_connection_patcher.start()
        self.client = self.processing.app.test_client()

    def tearDown(self):
        self.get_db_connection_patcher.stop()

    def test_accepts_exact_single_dry_contact_payload(self):
        payload = {
            "gatewayMessage": {
                "gatewayID": "1161104",
                "gatewayName": "HQ Cell",
                "accountID": "17221",
                "networkID": "28446",
                "messageType": "0",
                "power": "0",
                "batteryLevel": "101",
                "date": "2026-04-15 20:36:01",
                "count": "1",
                "signalStrength": "20",
                "pendingChange": "False",
            },
            "sensorMessages": [
                {
                    "sensorID": "1368042",
                    "sensorName": "Dry Contact - Air Cooled",
                    "applicationID": "3",
                    "networkID": "28446",
                    "dataMessageGUID": "fe8050db-cb92-43a1-b224-e8d16bb082c7",
                    "state": "2",
                    "messageDate": "2026-04-15 10:36:01",
                    "rawData": "False",
                    "dataType": "DryContact",
                    "dataValue": "False",
                    "plotValues": "0",
                    "plotLabels": "DryContact",
                    "batteryLevel": "100",
                    "signalStrength": "79",
                    "pendingChange": "True",
                    "voltage": "3.07",
                }
            ],
        }

        response = self.client.post("/imonnit-webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["counts"]["inserted"], 1)
        self.assertEqual(len(self.fake_db.rows), 1)
        self.assertEqual(self.fake_db.rows[0]["sensor_name"], "Dry Contact - Air Cooled")
        self.assertEqual(self.fake_db.rows[0]["state"], "Open")

    def test_mixed_payload_inserts_dry_contact_and_temperature_sensors(self):
        payload = {
            "gatewayMessage": {
                "gatewayID": "1161104",
                "gatewayName": "HQ Cell",
            },
            "sensorMessages": [
                {
                    "sensorID": "1368042",
                    "sensorName": "Dry Contact - Air Cooled",
                    "dataMessageGUID": "guid-air",
                    "messageDate": "2026-04-15 10:10:00",
                    "dataType": "DryContact",
                    "dataValue": "True",
                },
                {
                    "sensorID": "1361775",
                    "sensorName": "Dry Contact - Water Cooled",
                    "dataMessageGUID": "guid-water",
                    "messageDate": "2026-04-15 10:10:00",
                    "dataType": "DryContact",
                    "dataValue": "True",
                },
                {
                    "sensorID": "1366766",
                    "sensorName": "Dry Contact - Wrap",
                    "dataMessageGUID": "guid-wrap",
                    "messageDate": "2026-04-15 10:14:15",
                    "dataType": "DryContact",
                    "dataValue": "False",
                },
                {
                    "sensorID": "1319113",
                    "sensorName": "Freezer Main",
                    "dataMessageGUID": "guid-freezer",
                    "messageDate": "2026-04-15 10:10:00",
                    "dataType": "TemperatureData",
                    "dataValue": "-21.8",
                },
            ],
        }

        response = self.client.post("/imonnit-webhook", json=payload)
        body = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["counts"]["processed"], 4)
        self.assertEqual(body["counts"]["inserted"], 4)
        self.assertEqual(body["counts"]["skipped"], 0)
        self.assertEqual(len(self.fake_db.rows), 4)
        self.assertEqual(
            {row["sensor_name"] for row in self.fake_db.rows},
            {
                "Dry Contact - Air Cooled",
                "Dry Contact - Water Cooled",
                "Dry Contact - Wrap",
                "Freezer Main",
            },
        )

    def test_replayed_payload_is_counted_as_duplicate(self):
        payload = {
            "gatewayMessage": {"gatewayID": "1161104"},
            "sensorMessages": [
                {
                    "sensorID": "1368042",
                    "sensorName": "Dry Contact - Air Cooled",
                    "dataMessageGUID": "same-guid",
                    "messageDate": "2026-04-15 10:36:01",
                    "dataType": "DryContact",
                    "dataValue": "False",
                }
            ],
        }

        first = self.client.post("/imonnit-webhook", json=payload)
        second = self.client.post("/imonnit-webhook", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.get_json()["counts"]["duplicate"], 1)
        self.assertEqual(len(self.fake_db.rows), 1)

    def test_invalid_dry_contact_row_is_skipped_while_valid_row_commits(self):
        payload = {
            "gatewayMessage": {"gatewayID": "1161104"},
            "sensorMessages": [
                {
                    "sensorID": "1368042",
                    "sensorName": "Dry Contact - Air Cooled",
                    "dataMessageGUID": "valid-guid",
                    "messageDate": "2026-04-15 10:36:01",
                    "dataType": "DryContact",
                    "dataValue": "False",
                },
                {
                    "sensorID": "1361775",
                    "sensorName": "Dry Contact - Water Cooled",
                    "messageDate": "2026-04-15 10:26:44",
                    "dataType": "DryContact",
                    "dataValue": "False",
                },
            ],
        }

        response = self.client.post("/imonnit-webhook", json=payload)
        body = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["counts"]["inserted"], 1)
        self.assertEqual(body["counts"]["invalid"], 1)
        self.assertEqual(len(self.fake_db.rows), 1)

    def test_weekend_dry_contact_is_skipped_by_schedule(self):
        payload = {
            "gatewayMessage": {"gatewayID": "1161104"},
            "sensorMessages": [
                {
                    "sensorID": "1368042",
                    "sensorName": "Dry Contact - Air Cooled",
                    "dataMessageGUID": "weekend-guid",
                    "messageDate": "2026-04-17 10:00:00",
                    "dataType": "DryContact",
                    "dataValue": "False",
                }
            ],
        }

        response = self.client.post("/imonnit-webhook", json=payload)
        body = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["counts"]["processed"], 0)
        self.assertEqual(body["counts"]["skipped_schedule"], 1)
        self.assertEqual(body["counts"]["inserted"], 0)
        self.assertEqual(len(self.fake_db.rows), 0)

    def test_after_hours_dry_contact_is_skipped_by_schedule(self):
        payload = {
            "gatewayMessage": {"gatewayID": "1161104"},
            "sensorMessages": [
                {
                    "sensorID": "1361775",
                    "sensorName": "Dry Contact - Water Cooled",
                    "dataMessageGUID": "after-hours-guid",
                    "messageDate": "2026-04-13 03:30:00",
                    "dataType": "DryContact",
                    "dataValue": "True",
                }
            ],
        }

        response = self.client.post("/imonnit-webhook", json=payload)
        body = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(body["counts"]["processed"], 0)
        self.assertEqual(body["counts"]["skipped_schedule"], 1)
        self.assertEqual(body["counts"]["inserted"], 0)
        self.assertEqual(len(self.fake_db.rows), 0)


if __name__ == "__main__":
    unittest.main()
