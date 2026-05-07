"""Build the local King of Pops dashboard data files.

This script is the bridge between the Railway PostgreSQL event history and the
browser dashboard. Each run reads any new database rows, merges them with the
local daily history, computes dashboard metrics, and writes:

- data_YYYY-MM-DD.xlsx: raw local event history for the day
- gui_YYYY-MM-DD.xlsx: Excel compatibility export of dashboard metrics
- gui_YYYY-MM-DD.json: lightweight live file read by the browser every 5 seconds
- cache_YYYY-MM-DD.csv: fallback raw history only when the Excel file is locked

Run directly with `python sdtest.py`; server.py runs it automatically.
"""

import glob
import json
import os
import sys
from datetime import datetime

import pandas as pd
from sqlalchemy import create_engine


pd.options.mode.chained_assignment = None


# Local dashboard database connection. Set this before running server.py:
#   PowerShell: $env:KOP_DATABASE_URL="postgresql://..."
# DATABASE_URL is also accepted for deploy/hosting compatibility.
DB_URL = os.environ.get("KOP_DATABASE_URL") or os.environ.get("DATABASE_URL")

# Raw PostgreSQL/event columns expected by the dashboard processor.
EXPECTED_COLUMNS = [
    "id",
    "device_id",
    "sensor_name",
    "state",
    "timestamp",
    "event_date",
    "message_guid",
]
SENSORS = {
    "wrap": "Dry Contact - Wrap",
    "air": "Dry Contact - Air Cooled",
    "water": "Dry Contact - Water Cooled",
    "freezer_container": "Freezer Container",
    "freezer_main": "Freezer Main",
    "fridge_blend": "Fridge Blend",
    "fridge_hall": "Fridge Hall",
}


# File and table naming
# ---------------------
# The database stores daily partitions named like events_p_20260506. The local
# dashboard files use the readable YYYY-MM-DD date in their filenames.
def today_names():
    now = datetime.now()
    current_date = now.strftime("%Y-%m-%d")
    db_name = f"events_p_{now.year}{now.month:02d}{now.day:02d}"
    return (
        db_name,
        f"data_{current_date}.xlsx",
        f"gui_{current_date}.xlsx",
        f"gui_{current_date}.json",
        f"cache_{current_date}.csv",
    )


# Local history readers
# ---------------------
# The raw Excel file is the normal source of local history. The CSV cache exists
# only to recover rows when Excel/OneDrive has the workbook locked.
def empty_events():
    return pd.DataFrame(columns=EXPECTED_COLUMNS)


def read_events_file(path):
    try:
        df = pd.read_excel(path, index_col=0)
    except FileNotFoundError:
        return empty_events()
    except Exception as exc:
        print(f"[sdtest warning] Could not read {path}: {exc}", file=sys.stderr)
        return empty_events()

    return normalize_events(df)


def read_events_csv(path):
    try:
        df = pd.read_csv(path)
    except FileNotFoundError:
        return empty_events()
    except Exception as exc:
        print(f"[sdtest warning] Could not read {path}: {exc}", file=sys.stderr)
        return empty_events()

    return normalize_events(df)


def max_event_id(df):
    if df.empty or "id" not in df.columns:
        return 0
    value = df["id"].max()
    return 0 if pd.isna(value) else int(value)


def read_latest_local_events(today_file, cache_file):
    data = read_events_file(today_file)
    cache = read_events_csv(cache_file)

    if not data.empty and not cache.empty:
        if max_event_id(cache) > max_event_id(data):
            print(f"[sdtest] Using recovery cache from {cache_file}")
            return cache
        return data

    if not data.empty:
        return data

    if not cache.empty:
        print(f"[sdtest] Using recovery cache from {cache_file}")
        return cache

    candidates = [
        path
        for path in glob.glob("data_*.xlsx")
        if not os.path.basename(path).startswith("data_~$")
    ]
    candidates = sorted(candidates, key=os.path.getmtime, reverse=True)

    for path in candidates:
        if path == today_file:
            continue
        data = read_events_file(path)
        if not data.empty:
            print(f"[sdtest] Using local fallback data from {path}")
            return data

    return empty_events()


def merge_events(*frames):
    usable = [frame for frame in frames if frame is not None and not frame.empty]
    if not usable:
        return empty_events()
    return normalize_events(pd.concat(usable, ignore_index=True))


def write_excel_best_effort(df, path):
    try:
        df.to_excel(path)
        return True
    except PermissionError:
        print(
            f"[sdtest warning] Could not write {path}. Close the workbook if it is open.",
            file=sys.stderr,
        )
    except Exception as exc:
        print(f"[sdtest warning] Could not write {path}: {exc}", file=sys.stderr)
    return False


def write_cache_best_effort(df, path):
    try:
        df.to_csv(path, index=False)
        return True
    except Exception as exc:
        print(f"[sdtest warning] Could not write {path}: {exc}", file=sys.stderr)
    return False


def write_json_atomic(payload, path):
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, path)
        return True
    except Exception as exc:
        print(f"[sdtest warning] Could not write {path}: {exc}", file=sys.stderr)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
    return False


# Event normalization
# -------------------
# All inputs are reshaped into the same schema, deduplicated by event id, and
# sorted so later calculations can work from clean chronological history.
def normalize_events(df):
    if df is None or df.empty:
        return empty_events()

    df = df.copy()
    for column in EXPECTED_COLUMNS:
        if column not in df.columns:
            df[column] = pd.NA

    df = df[EXPECTED_COLUMNS]
    df["id"] = pd.to_numeric(df["id"], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    df = df.dropna(subset=["id"])
    df = df.drop_duplicates(subset=["id"], keep="last")
    return df.sort_values("id").reset_index(drop=True)


def fetch_new_events(db_name, last_id):
    if not DB_URL:
        raise RuntimeError(
            "KOP_DATABASE_URL or DATABASE_URL must be set to the Railway PostgreSQL connection string."
        )
    engine = create_engine(DB_URL, pool_pre_ping=True)
    query = f"SELECT * FROM {db_name} WHERE id > %(last_id)s;"
    return normalize_events(pd.read_sql(query, engine, params={"last_id": int(last_id)}))


# Production event helpers
# ------------------------
# Dry-contact Closed -> Open transitions are counted as production actions:
# pulls for the two freezing machines and wraps for the wrapping station.
def sensor_df(df, sensor_name):
    if df.empty:
        return empty_events()
    return df[df["sensor_name"] == sensor_name].copy()


def transition_events(df, from_state="Closed", to_state="Open", max_seconds=None):
    if df.empty:
        return empty_events()

    working = df.dropna(subset=["timestamp"]).sort_values(["timestamp", "id"]).copy()
    mask = (working["state"] == to_state) & (working["state"].shift(1) == from_state)
    if max_seconds is not None:
        time_diff = working["timestamp"].diff().abs()
        mask = mask & (time_diff <= pd.Timedelta(seconds=max_seconds))
    return working[mask].copy()


def recent_minutes(events, count=30):
    if events.empty:
        return [0] * count

    recent = events.tail(count).copy()
    minutes = [
        max((pd.Timestamp.now() - timestamp).total_seconds() / 60, 0)
        for timestamp in recent["timestamp"]
        if pd.notna(timestamp)
    ]
    return ([0] * max(count - len(minutes), 0) + minutes)[-count:]


def rate_per_minute(events):
    if events.empty:
        return 0

    first_timestamp = events["timestamp"].dropna().iloc[0] if events["timestamp"].notna().any() else None
    if first_timestamp is None:
        return 0

    elapsed_seconds = (pd.Timestamp.now() - first_timestamp).total_seconds()
    if elapsed_seconds <= 0:
        return 0
    return len(events) / elapsed_seconds * 60


def seconds_since_first_of_last(events, count=10):
    recent = events.tail(count)
    if recent.empty:
        return 0

    first_timestamp = recent["timestamp"].dropna().iloc[0] if recent["timestamp"].notna().any() else None
    if first_timestamp is None:
        return 0

    return abs((pd.Timestamp.now() - first_timestamp).total_seconds()) / len(recent)


# Dashboard metric calculations
# -----------------------------
# Pull intervals are seconds between consecutive pull events. Mold ages simulate
# freezer slots: each pull resets the oldest ready slot, so the ready list drops
# after a mold is pulled and grows again as molds reach 30 minutes.
def pull_intervals_sec(events, count=10):
    if events.empty:
        return []

    timestamps = events["timestamp"].dropna().sort_values()
    if len(timestamps) < 2:
        return []

    intervals = timestamps.diff().dropna().dt.total_seconds()
    return [float(round(value, 1)) for value in intervals.tail(count)]


def mold_ages_sec(events, now, count=30):
    if events.empty:
        return []

    loaded_at = []
    timestamps = events["timestamp"].dropna().sort_values()
    for timestamp in timestamps:
        ready_indexes = [
            i for i, loaded in enumerate(loaded_at)
            if (timestamp - loaded).total_seconds() >= 30 * 60
        ]
        if ready_indexes:
            oldest_ready = min(ready_indexes, key=lambda i: loaded_at[i])
            loaded_at[oldest_ready] = timestamp
        elif len(loaded_at) < count:
            loaded_at.append(timestamp)
        else:
            oldest = min(range(len(loaded_at)), key=lambda i: loaded_at[i])
            loaded_at[oldest] = timestamp

    ages = [
        max((now - timestamp).total_seconds(), 0)
        for timestamp in loaded_at
    ]
    return [float(round(value, 1)) for value in sorted(ages, reverse=True)]


def interval_average_sec(events, count=10):
    values = pull_intervals_sec(events, count)
    if not values:
        return 0
    return float(sum(values) / len(values))


# Output builders
# ---------------
# build_outputs keeps the legacy Excel export contract. build_live_payload is the
# richer JSON contract used by popsicle_dashboard.html for the 5-second live UI.
def latest_state(df):
    if df.empty:
        return []
    values = df.dropna(subset=["timestamp"]).sort_values(["timestamp", "id"])["state"].tail(1).tolist()
    return values


def latest_numeric_state(df):
    values = latest_state(df)
    if not values:
        return None
    try:
        return float(values[0])
    except (TypeError, ValueError):
        return None


def build_outputs(df):
    wrap_events = transition_events(sensor_df(df, SENSORS["wrap"]), max_seconds=61)
    air_events = transition_events(sensor_df(df, SENSORS["air"]))
    water_events = transition_events(sensor_df(df, SENSORS["water"]))

    recent_wrap = wrap_events[
        (pd.Timestamp.now() - wrap_events["timestamp"]) <= pd.Timedelta(minutes=30)
    ]

    total_air_pulled = 28 * len(air_events)
    total_water_pulled = 28 * len(water_events)

    outputs = {
        "Total_Pulled": total_air_pulled + total_water_pulled,
        "Total_Air_Pulled": total_air_pulled,
        "Total_Water_Pulled": total_water_pulled,
        "Total Wrapped": len(wrap_events) * 2,
        "30_min_wrap_rate": len(recent_wrap) / 30,
        "L10_air_rate": interval_average_sec(air_events),
        "L10_water_rate": interval_average_sec(water_events),
        "total_wrap_rate": rate_per_minute(wrap_events),
        "total_air_rate": rate_per_minute(air_events),
        "total_water_rate": rate_per_minute(water_events),
        "30_air_time": recent_minutes(air_events),
        "30_water_time": recent_minutes(water_events),
        "freezer_container": latest_state(sensor_df(df, SENSORS["freezer_container"])),
        "freezer_main": latest_state(sensor_df(df, SENSORS["freezer_main"])),
        "fridge_blend": latest_state(sensor_df(df, SENSORS["fridge_blend"])),
        "fridge_hall": latest_state(sensor_df(df, SENSORS["fridge_hall"])),
    }
    return pd.DataFrame.from_dict(outputs, orient="index")


def build_live_payload(df, db_status):
    now = pd.Timestamp.now()
    wrap_events = transition_events(sensor_df(df, SENSORS["wrap"]), max_seconds=61)
    air_events = transition_events(sensor_df(df, SENSORS["air"]))
    water_events = transition_events(sensor_df(df, SENSORS["water"]))

    recent_wrap = wrap_events[
        (now - wrap_events["timestamp"]) <= pd.Timedelta(minutes=30)
    ] if not wrap_events.empty else empty_events()

    total_air_pulled = 28 * len(air_events)
    total_water_pulled = 28 * len(water_events)

    return {
        "schema_version": 1,
        "generated_at": now.isoformat(),
        "db_status": db_status,
        "totals": {
            "total_pulled": total_air_pulled + total_water_pulled,
            "total_air_pulled": total_air_pulled,
            "total_water_pulled": total_water_pulled,
            "total_wrapped": len(wrap_events) * 2,
        },
        "rates": {
            "wrap_rate_30min": len(recent_wrap) / 30,
            "l10_air_interval_sec": interval_average_sec(air_events),
            "l10_water_interval_sec": interval_average_sec(water_events),
            "total_wrap_rate": rate_per_minute(wrap_events),
            "total_air_rate": rate_per_minute(air_events),
            "total_water_rate": rate_per_minute(water_events),
        },
        "air_mold_ages_sec": mold_ages_sec(air_events, now),
        "water_mold_ages_sec": mold_ages_sec(water_events, now),
        "air_pull_intervals_sec": pull_intervals_sec(air_events),
        "water_pull_intervals_sec": pull_intervals_sec(water_events),
        "temperatures_c": {
            "freezer_container": latest_numeric_state(sensor_df(df, SENSORS["freezer_container"])),
            "freezer_main": latest_numeric_state(sensor_df(df, SENSORS["freezer_main"])),
            "fridge_blend": latest_numeric_state(sensor_df(df, SENSORS["fridge_blend"])),
            "fridge_hall": latest_numeric_state(sensor_df(df, SENSORS["fridge_hall"])),
        },
    }


# Main refresh
# ------------
# Every run reads local history, pulls only newer database rows, writes the live
# JSON atomically, and updates Excel outputs best-effort.
def refresh_data():
    db_name, data_file, gui_file, json_file, cache_file = today_names()
    local_data = read_latest_local_events(data_file, cache_file)
    last_id = max_event_id(local_data)

    try:
        new_data = fetch_new_events(db_name, last_id)
        print(f"[sdtest] Pulled {len(new_data)} new rows from {db_name}.")
        db_status = {"ok": True, "table": db_name, "new_rows": len(new_data), "error": ""}
    except Exception as exc:
        print(f"[sdtest warning] Database refresh failed: {exc}", file=sys.stderr)
        db_status = {"ok": False, "table": db_name, "new_rows": 0, "error": str(exc)}
        new_data = empty_events()

    df = merge_events(local_data, new_data)
    if df.empty:
        print("[sdtest warning] No event data available. Writing empty dashboard values.", file=sys.stderr)

    outputs = build_outputs(df)
    live_payload = build_live_payload(df, db_status)
    json_written = write_json_atomic(live_payload, json_file)
    data_written = write_excel_best_effort(df, data_file)
    if not data_written:
        write_cache_best_effort(df, cache_file)
    gui_written = write_excel_best_effort(outputs, gui_file)

    if gui_written:
        print(f"[sdtest] Wrote {gui_file} with {len(df)} total rows.")
    else:
        print(f"[sdtest warning] Dashboard export {gui_file} was not updated.", file=sys.stderr)
    if json_written:
        print(f"[sdtest] Wrote {json_file}.")
    if not data_written:
        print(f"[sdtest] Internal cache is still updated in {cache_file}.")


if __name__ == "__main__":
    refresh_data()
