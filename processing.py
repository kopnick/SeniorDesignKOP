"""Railway webhook app for the King of Pops iMonnit data pipeline.

This Flask app receives iMonnit webhook payloads, accepts only the configured
production sensors during the configured production window, and stores each event
in Railway PostgreSQL. The database is kept as a partitioned `events` table with
one daily partition per retained production day. Duplicate messages are ignored
using message GUID and device/sensor/timestamp uniqueness. The `/latest` endpoint
is a small inspection endpoint that returns the newest stored events.
"""

from flask import Flask, request, jsonify
import os
from datetime import datetime, timezone, timedelta
from datetime import time
import psycopg2
from psycopg2.extras import RealDictCursor

# Sensors and production schedule that control what gets stored in PostgreSQL.
# To add or remove sensors later, update this list and review classify_sensor().
DRY_CONTACT_NAMES = (
    "Dry Contact - Wrap",
    "Dry Contact - Air Cooled",
    "Dry Contact - Water Cooled",
    "Freezer Container",
    "Freezer Main",
    "Fridge Blend",
    "Fridge Hall"
)

REQUIRED_DRY_CONTACT_FIELDS = (
    "sensorID",
    "sensorName",
    "messageDate",
    "dataMessageGUID",
    "dataType",
    "dataValue",
)

RETAINED_PRODUCTION_DAYS = 8  # Current PostgreSQL retention window.
PARTITION_PREFIX = "events_p_"
PRODUCTION_START_TIME = time(6, 0, 0)  # 6:00 AM local production start.
PRODUCTION_END_TIME = time(22, 0, 0)  # 10:00 PM local production end.
PRODUCTION_WEEKDAYS = {0, 1, 2, 3, 6}  # Monday-Thursday and Sunday.

app = Flask(__name__)

def to_est(utc_str):
    """Convert UTC timestamp string from Monnit to EST/EDT."""
    try:
        dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S")
        dt = dt.replace(tzinfo=timezone.utc)
        et_offset = timedelta(hours=-4)  # EDT (summer), change to -5 for EST (winter)
        return (dt + et_offset).strftime("%Y-%m-%d %H:%M:%S")
    except:
        return utc_str  # return original if conversion fails
        
# PostgreSQL connection. Railway supplies these environment variables to the app.
def get_db_connection():
    return psycopg2.connect(
        host=os.environ.get("PGHOST"),
        database=os.environ.get("PGDATABASE"),
        user=os.environ.get("PGUSER"),
        password=os.environ.get("PGPASSWORD"),
        port=os.environ.get("PGPORT"),
        cursor_factory=RealDictCursor,
    )


def is_blank(value):
    return value is None or (isinstance(value, str) and value.strip() == "")


def is_dry_contact_sensor(sensor_name):
    return any(sensor_name == name for name in DRY_CONTACT_NAMES)


def parse_dry_contact_state(raw_state):
    if raw_state == "True":
        return "Closed"
    if raw_state == "False":
        return "Open"
    try:
        dummy = float(raw_state)
        return raw_state
    except:
        return None


def classify_sensor(sensor):
    """Return whether one incoming sensor message should be stored."""
    sensor_name = sensor.get("sensorName", "")
    data_type = sensor.get("dataType")

    if not is_dry_contact_sensor(sensor_name):
        return {
            "status": "skipped",
            "reason": f"sensor '{sensor_name}' is not one of the configured dry contact sensors",
        }

    missing = [field for field in REQUIRED_DRY_CONTACT_FIELDS if is_blank(sensor.get(field))]
    if missing:
        return {
            "status": "invalid",
            "reason": f"missing required fields: {', '.join(missing)}",
        }

    if data_type != "DryContact":
        if data_type != "TemperatureData":
            return {"status": "invalid","reason": f"unexpected dataType '{data_type}'",}

    state = parse_dry_contact_state(sensor.get("dataValue"))
    if state is None:
        return {
            "status": "invalid",
            "reason": f"unexpected dataValue '{sensor.get('dataValue')}'",
        }

    return {"status": "processed", "state": state}


def parse_message_timestamp(raw_timestamp):
    if is_blank(raw_timestamp):
        return None

    try:
        parsed = datetime.strptime(raw_timestamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None

    return parsed


def is_production_window(timestamp_value):
    if timestamp_value.weekday() not in PRODUCTION_WEEKDAYS:
        return False
    return PRODUCTION_START_TIME <= timestamp_value.time() <= PRODUCTION_END_TIME


def is_production_date(date_value):
    return date_value.weekday() in PRODUCTION_WEEKDAYS


def recent_production_dates(reference_date, count=RETAINED_PRODUCTION_DAYS):
    """Return the newest production dates that should have PostgreSQL partitions."""
    dates = []
    current = reference_date

    while len(dates) < count:
        if is_production_date(current):
            dates.append(current)
        current -= timedelta(days=1)

    return dates


def partition_table_name(event_date):
    return f"{PARTITION_PREFIX}{event_date.strftime('%Y%m%d')}"


def get_events_table_info(cur):
    cur.execute(
        """
        SELECT c.relname, c.relkind, pt.partstrat
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_partitioned_table pt ON pt.partrelid = c.oid
        WHERE n.nspname = current_schema() AND c.relname = 'events';
        """
    )
    return cur.fetchone()


def list_event_partitions(cur):
    cur.execute(
        """
        SELECT c.relname AS partition_name
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE p.relname = 'events'
          AND n.nspname = current_schema()
        ORDER BY c.relname;
        """
    )
    return [row["partition_name"] for row in cur.fetchall()]


def ensure_events_parent_table(cur):
    """Create or migrate the parent events table to the partitioned layout."""
    info = get_events_table_info(cur)
    print(f"EVENTS_TABLE_BEFORE: {info}", flush=True)

    if info and info["relkind"] == "p":
        return

    if info:
        cur.execute("ALTER TABLE events RENAME TO events_legacy;")
        print("EVENTS_TABLE_RENAMED_TO_LEGACY", flush=True)

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id           BIGINT GENERATED BY DEFAULT AS IDENTITY,
            device_id    INTEGER,
            sensor_name  TEXT,
            state        TEXT,
            timestamp    TIMESTAMP NOT NULL,
            event_date   DATE NOT NULL,
            message_guid TEXT
        ) PARTITION BY RANGE (event_date);
    """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS events_timestamp_idx
        ON events (timestamp DESC);
    """
    )

    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS events_message_guid_event_date_key
        ON events (message_guid, event_date);
    """
    )

    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS events_device_sensor_timestamp_event_date_key
        ON events (device_id, sensor_name, timestamp, event_date);
    """
    )

    if info:
        migrate_legacy_events(cur)

    info_after = get_events_table_info(cur)
    print(f"EVENTS_TABLE_AFTER: {info_after}", flush=True)
    if not info_after or info_after["relkind"] != "p":
        raise RuntimeError(
            f"events table migration failed: expected partitioned table, found {info_after}"
        )


def migrate_legacy_events(cur):
    """Move rows from an older non-partitioned events table into partitions."""
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = current_schema()
              AND table_name = 'events_legacy'
        ) AS exists;
        """
    )
    legacy = cur.fetchone()
    if not legacy or not legacy["exists"]:
        return

    cur.execute(
        """
        SELECT DISTINCT CAST(NULLIF(BTRIM(timestamp::text), '') AS timestamp)::date AS event_date
        FROM events_legacy
        WHERE NULLIF(BTRIM(timestamp::text), '') IS NOT NULL;
        """
    )
    for row in cur.fetchall():
        if row["event_date"] is not None:
            ensure_partition_for_date(cur, row["event_date"])

    cur.execute(
        """
        INSERT INTO events (device_id, sensor_name, state, timestamp, event_date, message_guid)
        SELECT
            device_id,
            sensor_name,
            state,
            CAST(NULLIF(BTRIM(timestamp::text), '') AS timestamp),
            CAST(NULLIF(BTRIM(timestamp::text), '') AS timestamp)::date,
            message_guid
        FROM events_legacy
        WHERE NULLIF(BTRIM(timestamp::text), '') IS NOT NULL
        ON CONFLICT DO NOTHING;
        """
    )

    cur.execute("DROP TABLE events_legacy;")


def ensure_partition_for_date(cur, event_date):
    """Create the daily events partition and indexes for a production date."""
    partition_name = partition_table_name(event_date)
    start_date = event_date
    end_date = event_date + timedelta(days=1)

    cur.execute(
        f"""
            CREATE TABLE IF NOT EXISTS {partition_name}
            PARTITION OF events
            FOR VALUES FROM (%s) TO (%s);
            """,
        (start_date, end_date),
    )

    cur.execute(
        f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {partition_name}_guid_date_key
            ON {partition_name} (message_guid, event_date);
            """
    )

    cur.execute(
        f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {partition_name}_device_sensor_ts_date_key
            ON {partition_name} (device_id, sensor_name, timestamp, event_date);
            """
    )


def drop_expired_partitions(cur, keep_dates):
    """Drop daily partitions outside the current retention window."""
    keep_date_set = set(keep_dates)

    cur.execute(
        """
        SELECT c.relname AS partition_name
        FROM pg_inherits i
        JOIN pg_class c ON c.oid = i.inhrelid
        JOIN pg_class p ON p.oid = i.inhparent
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE p.relname = 'events'
          AND n.nspname = current_schema()
          AND c.relname LIKE %s;
        """,
        (f"{PARTITION_PREFIX}%",),
    )

    for row in cur.fetchall():
        partition_name = row["partition_name"]
        suffix = partition_name.replace(PARTITION_PREFIX, "", 1)
        try:
            partition_date = datetime.strptime(suffix, "%Y%m%d").date()
        except ValueError:
            continue

        if partition_date not in keep_date_set:
            cur.execute(f"DROP TABLE IF EXISTS {partition_name};")


def init_db():
    """Verify the database schema and retained partitions when the app starts."""
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute("SELECT current_database() AS db_name, current_schema() AS schema_name;")
    db_context = cur.fetchone()
    print(f"DB_CONTEXT: {db_context}", flush=True)

    ensure_events_parent_table(cur)

    today = datetime.now().date()
    keep_dates = recent_production_dates(today)
    print(f"TARGET_PRODUCTION_DATES: {[d.isoformat() for d in keep_dates]}", flush=True)
    for production_date in keep_dates:
        ensure_partition_for_date(cur, production_date)

    drop_expired_partitions(cur, keep_dates)
    print(f"EVENT_PARTITIONS_AFTER_INIT: {list_event_partitions(cur)}", flush=True)

    conn.commit()
    print("Partitioned 'events' table verified/created successfully", flush=True)
    cur.close()
    conn.close()


init_db()


# iMonnit -> Railway endpoint. Each request may contain many sensor messages.
@app.route("/imonnit-webhook", methods=["POST"])
def webhook():
    print("WEBHOOK FUNCTION CALLED", flush=True)
    data = request.get_json(silent=True)

    if not data:
        return jsonify({"status": "error", "message": "No data"}), 400

    print(f"Data Received: {data}", flush=True)

    gateway_message = data.get("gatewayMessage")
    if not isinstance(gateway_message, dict):
        print("PAYLOAD INVALID: missing or invalid gatewayMessage", flush=True)
        return jsonify({"status": "error", "message": "Missing required field: gatewayMessage"}), 400

    sensor_messages = data.get("sensorMessages")
    if not isinstance(sensor_messages, list):
        print("PAYLOAD INVALID: missing or invalid top-level sensorMessages", flush=True)
        return jsonify({"status": "error", "message": "Missing or invalid field: sensorMessages"}), 400

    print(
        f"Payload validated: gatewayID={gateway_message.get('gatewayID')}, sensor_count={len(sensor_messages)}",
        flush=True,
    )
    print("About to establish database connection", flush=True)

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        print("DB connection established successfully", flush=True)
    except Exception as e:
        print(f"ERROR: Failed to connect to database: {e}", flush=True)
        return jsonify(
            {"status": "error", "message": "Database connection failed", "detail": str(e)}
        ), 500

    counts = {
        "processed": 0,
        "skipped": 0,
        "skipped_schedule": 0,
        "invalid": 0,
        "inserted": 0,
        "duplicate": 0,
        "failed": 0,
    }

    for index, sensor in enumerate(sensor_messages, start=1):
        sensor_name = sensor.get("sensorName", "")
        sensor_id = sensor.get("sensorID")
        print(
            f"Processing sensor {index}: sensorName='{sensor_name}', sensorID={sensor_id}",
            flush=True,
        )

        classification = classify_sensor(sensor)
        status = classification["status"]
        reason = classification.get("reason")

        if status == "skipped":
            counts["skipped"] += 1
            print(f"SKIPPED: sensorName='{sensor_name}' reason={reason}", flush=True)
            continue

        if status == "invalid":
            counts["invalid"] += 1
            print(f"INVALID: sensorName='{sensor_name}' reason={reason}", flush=True)
            continue

        counts["processed"] += 1

        message_date = to_est(sensor.get("messageDate", ""))
        message_guid = sensor.get("dataMessageGUID")
        state = classification["state"]
        parsed_timestamp = parse_message_timestamp(message_date)

        if parsed_timestamp is None:
            counts["invalid"] += 1
            counts["processed"] -= 1
            print(
                f"INVALID: sensorName='{sensor_name}' reason=unexpected messageDate '{message_date}'",
                flush=True,
            )
            continue

        if not is_production_window(parsed_timestamp):
            counts["skipped_schedule"] += 1
            counts["processed"] -= 1
            print(
                "SKIPPED_SCHEDULE: "
                f"sensorName='{sensor_name}' timestamp='{parsed_timestamp.isoformat(sep=' ')}'",
                flush=True,
            )
            continue

        event_date = parsed_timestamp.date()

        print(
            "PROCESSED: "
            f"sensorName='{sensor_name}', message_guid='{message_guid}', "
            f"state='{state}', timestamp='{parsed_timestamp.isoformat(sep=' ')}'",
            flush=True,
        )

        try:
            ensure_partition_for_date(cur, event_date)
            reference_date = max(datetime.now().date(), event_date)
            drop_expired_partitions(cur, recent_production_dates(reference_date))
            cur.execute(
                """
                INSERT INTO events (device_id, sensor_name, state, timestamp, event_date, message_guid)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (sensor_id, sensor_name, state, parsed_timestamp, event_date, message_guid),
            )

            if cur.rowcount == 1:
                counts["inserted"] += 1
                print(
                    f"INSERTED: sensorName='{sensor_name}', message_guid='{message_guid}'",
                    flush=True,
                )
            else:
                counts["duplicate"] += 1
                print(
                    "DUPLICATE: "
                    f"sensorName='{sensor_name}', message_guid='{message_guid}', timestamp='{parsed_timestamp.isoformat(sep=' ')}'",
                    flush=True,
                )
        except Exception as insert_error:
            counts["failed"] += 1
            print(
                f"FAILED: sensorName='{sensor_name}', error={type(insert_error).__name__}: {insert_error}",
                flush=True,
            )
            conn.rollback()
            cur = conn.cursor()

    print(f"Loop complete: {counts}", flush=True)

    try:
        conn.commit()
        print(f"Commit succeeded: {counts}", flush=True)
    except Exception as e:
        print(f"ERROR: Commit failed: {e}", flush=True)
        cur.close()
        conn.close()
        return jsonify(
            {"status": "error", "message": "Database commit failed", "detail": str(e)}
        ), 500

    cur.close()
    conn.close()

    return (
        jsonify(
            {
                "status": "success",
                "message": "Webhook processed",
                "counts": counts,
            }
        ),
        200,
    )


# Lightweight inspection endpoint for checking the newest stored events.
@app.route("/latest", methods=["GET"])
def latest():
    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT device_id, sensor_name, state, timestamp, message_guid
        FROM events
        ORDER BY timestamp DESC, id DESC
        LIMIT 50;
    """
    )

    rows = cur.fetchall()
    for row in rows:
        if row.get("timestamp") is not None:
            row["timestamp"] = row["timestamp"].strftime("%Y-%m-%d %H:%M:%S")

    cur.close()
    conn.close()

    return jsonify({"events": rows})
