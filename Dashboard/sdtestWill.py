import pandas as pd
from sqlalchemy import create_engine
import os
from datetime import datetime, timedelta
import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)

# --- CONFIGURATION ---
DB_URL = "postgresql://postgres:QbcDZnDSLdphWUiKWtMnpTuoZgYvGUpm@monorail.proxy.rlwy.net:10094/railway"

# Hardcoded targets — adjust these to match your shift goals
PRODUCTION_GOAL   = 5000   # total pops goal for the shift
PULL_TARGET_MIN   = 2.0    # target minutes between pulls (per machine)
WRAP_TARGET_RATE  = 2.0    # target wraps per minute

now = datetime.now()
day   = now.day
month = now.month
year  = now.year

db_name      = f'events_p_{year:04d}{month:02d}{day:02d}'
current_date = now.strftime('%Y-%m-%d')
filename     = f"data_{current_date}.xlsx"
guifilename  = f"gui_{current_date}.xlsx"
CSV_FILE     = "data.csv"          # dashboard reads this


def refresh_data():

    # ── Load cached data ──────────────────────────────────────────────────
    try:
        data    = pd.read_excel(filename, index_col=0)
        last_id = data['id'].max()
    except:
        data    = pd.DataFrame()
        last_id = 0

    # ── Pull new rows from Railway ────────────────────────────────────────
    try:
        engine      = create_engine(DB_URL)
        SQL_QUERY   = f"SELECT * FROM {db_name} WHERE id > %(last_id)s;"
        df          = pd.read_sql(SQL_QUERY, engine, params={'last_id': last_id})
    except:
        df = pd.DataFrame()
        print('DB connection error — using cached data only')

    df = pd.concat([data, df], ignore_index=True)
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    try:
        df = df.sort_values('id')
    except:
        pass

    # ── Split by sensor ──────────────────────────────────────────────────
    wrap_df  = df[df['sensor_name'] == 'Dry Contact - Wrap'].copy()
    air_df   = df[df['sensor_name'] == 'Dry Contact - Air Cooled'].copy()
    water_df = df[df['sensor_name'] == 'Dry Contact - Water Cooled'].copy()

    # ── Wraps: count Open←Closed transitions within 61 s ────────────────
    time_diff = abs(wrap_df['timestamp'].diff())
    wrap_mask = (
        (wrap_df['state'] == 'Open') &
        (wrap_df['state'].shift(1) == 'Closed') &
        (time_diff <= pd.Timedelta(seconds=61))
    )
    wrap_events  = wrap_df[wrap_mask]
    total_wrapped = len(wrap_events) * 2   # 2 pops per wrap cycle

    # ── Pull events ──────────────────────────────────────────────────────
    air_pull_mask = (
        (air_df['state'] == 'Open') &
        (air_df['state'].shift(1) == 'Closed')
    )
    air_events = air_df[air_pull_mask]

    water_pull_mask = (
        (water_df['state'] == 'Open') &
        (water_df['state'].shift(1) == 'Closed')
    )
    water_events = water_df[water_pull_mask]

    total_air_pulled   = 28 * len(air_events)
    total_water_pulled = 28 * len(water_events)
    total_pulled       = total_air_pulled + total_water_pulled

    # ── 30 most-recent pull intervals (minutes since each pull) ──────────
    timenow = pd.Timestamp.now()

    def last_n_intervals(events, n=30):
        if len(events) == 0:
            return [0] * n
        recent = events.tail(n).copy()
        recent['time_diff'] = timenow - recent['timestamp']
        mins = [td.total_seconds() / 60 for td in recent['time_diff']]
        if len(mins) < n:
            mins = [0] * (n - len(mins)) + mins
        return mins

    air_minutes   = last_n_intervals(air_events, 30)
    water_minutes = last_n_intervals(water_events, 30)

    # Combined pull history for dashboard (interleaved; last 30 meaningful values)
    # Use air_minutes as the primary pull history chart (air cooled machine)
    pull_history = air_minutes

    # ── Rates ────────────────────────────────────────────────────────────
    def event_rate(events):
        """Returns events per minute over the full history."""
        if len(events) < 2:
            return 0.0
        span = (timenow - events['timestamp'].iloc[0]).total_seconds()
        return len(events) / span * 60 if span > 0 else 0.0

    def last_n_rate(events, n=10):
        """Returns events per minute based on last n events."""
        if len(events) < 2:
            return 0.0
        tail = events.tail(n)
        span = (timenow - tail['timestamp'].iloc[0]).total_seconds()
        return len(tail) / span * 60 if span > 0 else 0.0

    air_rate         = event_rate(air_events)
    water_rate       = event_rate(water_events)
    wrap_rate        = event_rate(wrap_events)
    last_10_air_rate = last_n_rate(air_events, 10)
    last_10_water_rate = last_n_rate(water_events, 10)

    # Pull interval in minutes (inverse of rate; lower = faster)
    pull_shift_interval = (1 / (air_rate + water_rate)) if (air_rate + water_rate) > 0 else None
    l10_pull_interval   = (1 / (last_10_air_rate + last_10_water_rate)) \
                          if (last_10_air_rate + last_10_water_rate) > 0 else None

    # Wrap rate (wraps per minute)
    wrap_shift_rate = wrap_rate

    # ── Last-30-min wrap rate ─────────────────────────────────────────────
    try:
        cutoff = timenow - pd.Timedelta(minutes=30)
        last_30_min_wrap = wrap_events[wrap_events['timestamp'] >= cutoff]
        last_30_wrap_rate = len(last_30_min_wrap) / 30
    except:
        last_30_wrap_rate = 0

    # ── Save full raw data cache ──────────────────────────────────────────
    df.to_excel(filename)

    # ── Write gui XLSX (same schema as before) ───────────────────────────
    outputs = {
        'Total_Pulled':       total_pulled,
        'Total_Air_Pulled':   total_air_pulled,
        'Total_Water_Pulled': total_water_pulled,
        'Total Wrapped':      total_wrapped,
        '30_min_wrap_rate':   last_30_wrap_rate,
        'L10_air_rate':       last_10_air_rate,
        'L10_water_rate':     last_10_water_rate,
        'total_wrap_rate':    wrap_rate,
        'total_air_rate':     air_rate,
        'total_water_rate':   water_rate,
        '30_air_time':        air_minutes,
        '30_water_time':      water_minutes,
    }
    pd.DataFrame.from_dict(outputs, orient='index').to_excel(guifilename)

    # ── Write data.csv for the dashboard ─────────────────────────────────
    # pull_history: the 30 most recent air pull intervals (minutes)
    # wrap_history: the 30 most recent per-wrap intervals (minutes since each wrap)
    wrap_intervals = last_n_intervals(wrap_events, 30)

    rows = [
        ('shift',                 now.strftime('%a %I:%M %p')),
        ('flavor',                '—'),
        ('wrapped_today',         int(total_wrapped)),
        ('production_goal',       int(PRODUCTION_GOAL)),
        ('frozen_today',          int(total_pulled)),       # pulled ≈ frozen
        ('pull_target',           round(PULL_TARGET_MIN, 2)),
        ('pull_shift',            round(pull_shift_interval, 2) if pull_shift_interval else ''),
        ('pull_history',          ','.join(f'{v:.2f}' for v in pull_history)),
        ('wrap_target',           round(WRAP_TARGET_RATE, 2)),
        ('wrap_shift',            round(wrap_shift_rate, 3)),
        ('wrap_history',          ','.join(f'{v:.2f}' for v in wrap_intervals)),
        ('freezer_a_freezing_temp', -18),
        ('freezer_b_freezing_temp', -18),
        ('freezer_a_molds',       ''),   # no mold temp sensors yet
        ('freezer_b_molds',       ''),
    ]

    with open(CSV_FILE, 'w') as f:
        print('hit')
        f.write('key,value\n')
        for key, val in rows:
            f.write(f'{key},{val}\n')

    print(f"✓ Refreshed at {timenow.strftime('%H:%M:%S')} — "
          f"{total_pulled} pulled | {total_wrapped} wrapped | "
          f"air rate {air_rate:.3f}/min | wrap rate {wrap_rate:.3f}/min")


#if __name__ == "__main__":
#refresh_data()
