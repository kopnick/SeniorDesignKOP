# SeniorDesignKOP

Client handoff guide for the King of Pops iMonnit-to-dashboard data pipeline.

## What This System Does

This project keeps the production dashboard updated from live iMonnit sensor data.
At a high level, the flow is:

```text
iMonnit sensors
  -> iMonnit webhook
  -> Railway Flask app in processing.py
  -> Railway PostgreSQL events tables
  -> local Dashboard/server.py
  -> Dashboard/sdtest.py every 5 seconds
  -> local data_YYYY-MM-DD.xlsx history file
  -> local gui_YYYY-MM-DD.json live dashboard file
  -> Dashboard/popsicle_dashboard.html in the browser
```

The Railway app receives sensor messages, keeps only the configured production
sensors and production-time events, and stores them in PostgreSQL. The local
dashboard folder then reads from PostgreSQL, calculates production metrics,
writes local history/output files, and lets the browser reload the latest JSON
dashboard state every 5 seconds.

The browser uses `gui_YYYY-MM-DD.json` as the live source of truth because it is
small and safe to refresh often. Excel files are still written for raw history,
compatibility, and debugging.

## Main Pieces

- `processing.py`: Railway Flask app. It receives iMonnit webhook posts at
  `/imonnit-webhook`, validates the payload, filters by sensor and production
  schedule, creates daily PostgreSQL partitions, avoids duplicate inserts, and
  stores events.
- `main.py` and `Procfile`: Railway startup files. Railway runs the Flask app
  through Gunicorn using `main:app`.
- Railway PostgreSQL: Stores the `events` parent table and daily partition tables
  named like `events_p_20260506`.
- `Dashboard/`: Source of truth for the local dashboard files.
- `Dashboard/server.py`: Starts a local web server, runs `sdtest.py` immediately,
  then reruns it every 5 seconds. If port `8000` is busy, it tries the next
  available port and prints the exact URL to open.
- `Dashboard/sdtest.py`: Reads new rows from PostgreSQL, merges them with local
  daily history, calculates dashboard metrics, simulates freezer mold readiness,
  writes the live JSON file atomically, and updates Excel outputs best-effort.
- `Dashboard/popsicle_dashboard.html`: The visual dashboard. It reloads the
  current day's `gui_YYYY-MM-DD.json` file every 5 seconds, falls back to
  `gui_YYYY-MM-DD.xlsx` if JSON is missing, and updates ready-to-pull timers
  every second in the browser.

## Normal Operating Procedure

Railway should stay deployed and connected to the GitHub repository. When iMonnit
sends data to the Railway webhook, Railway writes accepted events to PostgreSQL.

On the dashboard computer:

1. Install dependencies if needed:

   ```powershell
   pip install -r requirements.txt
   ```

2. Set the local dashboard database connection string. Do not commit this value
   to GitHub.

   ```powershell
   $env:KOP_DATABASE_URL="postgresql://USER:PASSWORD@HOST:PORT/DATABASE"
   ```

   `DATABASE_URL` also works if that is easier for the machine setup.

3. Open PowerShell in the `Dashboard` folder.

4. Start the local dashboard server:

   ```powershell
   python server.py
   ```

5. Open the URL printed by the server, usually:

   ```text
   http://localhost:8000/popsicle_dashboard.html
   ```

Leave the PowerShell window open while the dashboard is in use. `server.py` runs
`sdtest.py` immediately, then again every 5 seconds. The browser also reloads
the latest `gui_YYYY-MM-DD.json` file every 5 seconds.

Important: Use the `http://localhost:.../popsicle_dashboard.html` address for
normal operation. Do not open `popsicle_dashboard.html` directly from the file
browser with a `file:///` address. The local server is what lets the browser load
the latest generated JSON and Excel files consistently.

## Generated Local Files

These files are created inside `Dashboard/` during normal use and should not be
committed:

- `data_YYYY-MM-DD.xlsx`: Raw local history of the day's PostgreSQL rows. This
  lets `sdtest.py` request only newer rows on later refreshes.
- `gui_YYYY-MM-DD.json`: Live dashboard state read by the browser every 5
  seconds. This includes totals, rates, freezer mold ages, pull intervals,
  temperatures, generated timestamp, and database refresh status.
- `gui_YYYY-MM-DD.xlsx`: Dashboard-ready Excel compatibility export.
- `cache_YYYY-MM-DD.csv`: Recovery copy of raw event history. This is only needed
  when Excel or OneDrive locks `data_YYYY-MM-DD.xlsx`.
- `server_live*.log`: Optional local server logs if a background process is used.

## Future Changes

- PostgreSQL retention window: Change `RETAINED_PRODUCTION_DAYS = 8` in
  `processing.py`. This controls how many production days of daily PostgreSQL
  partitions are kept.
- Production schedule: Change `PRODUCTION_START_TIME`, `PRODUCTION_END_TIME`,
  and `PRODUCTION_WEEKDAYS` in `processing.py`.
- Sensors stored in PostgreSQL: Update `DRY_CONTACT_NAMES` and review
  `classify_sensor()` in `processing.py`.
- Local dashboard database connection: Set `KOP_DATABASE_URL` or `DATABASE_URL`
  on the dashboard computer. Do not hard-code credentials in `Dashboard/sdtest.py`.
- Dashboard metric logic: Update `SENSORS`, `transition_events()`,
  `build_outputs()`, and `build_live_payload()` in `Dashboard/sdtest.py`.
- Freezer readiness logic: Update `mold_ages_sec()` in `Dashboard/sdtest.py`.
  The current model treats each pull event as a new mold load and resets the
  oldest ready slot once a pull occurs.
- Dashboard display bindings: If `sdtest.py` writes new or renamed JSON fields,
  update `parseJSONPayload()` in `Dashboard/popsicle_dashboard.html`. The XLSX
  fallback parser is `parseXLSXBuffer()`.
- Refresh frequency: Change `REFRESH_SEC` in `Dashboard/server.py` for the Python
  database refresh loop and `REFRESH_MS` in `Dashboard/popsicle_dashboard.html`
  for the browser JSON reload interval.
- Ready-to-pull thresholds: Change `READY_SEC` and `WARNING_SEC` in
  `Dashboard/popsicle_dashboard.html` for the green/yellow/red freezer display.

## Troubleshooting

- Dashboard page will not open: Make sure `python server.py` is still running and
  open the exact URL printed by the server.
- Correct dashboard does not appear: Stop the server with `Ctrl+C`, restart
  `python server.py` from inside the `Dashboard` folder, then hard-refresh the
  browser with `Ctrl+F5`.
- Dashboard opened from the file browser does not update: Close that tab and use
  the local `http://localhost:.../popsicle_dashboard.html` URL instead of the
  `file:///` version.
- Dashboard says the JSON file is missing: Run `python server.py` from inside the
  `Dashboard` folder and confirm `gui_YYYY-MM-DD.json` is being created.
- No new data appears: Confirm Railway is deployed, iMonnit is sending webhook
  data, the current event time falls within the configured production window, and
  `KOP_DATABASE_URL` or `DATABASE_URL` is set on the dashboard computer.
- Database connection error: Confirm the local dashboard connection string is
  current and that the network allows outbound TCP to Railway PostgreSQL.
- Raw Excel history does not update: Close `data_YYYY-MM-DD.xlsx` if it is open
  in Excel. The dashboard can still update from JSON, and `cache_YYYY-MM-DD.csv`
  protects raw rows until Excel is writable again.
- Ready mold counts look stale: Confirm only one `server.py` process is running.
  Multiple local servers will multiply database queries and file writes.
- Only old data appears: Archive the current day's generated local files in
  `Dashboard/`, then restart `server.py` so `sdtest.py` rebuilds them from
  PostgreSQL.

## Developer Notes

The deployed Railway app uses environment variables named `PGHOST`,
`PGDATABASE`, `PGUSER`, `PGPASSWORD`, and `PGPORT` for PostgreSQL. The local
dashboard processor uses `KOP_DATABASE_URL` or `DATABASE_URL`.

Run these checks after code or documentation changes:

```powershell
python -m py_compile processing.py Dashboard/server.py Dashboard/sdtest.py
python test_processing.py
```

For dashboard-only changes, also run:

```powershell
cd Dashboard
python sdtest.py
python server.py
```

Then open the printed localhost URL and confirm the browser is loading
`gui_YYYY-MM-DD.json` every 5 seconds.
