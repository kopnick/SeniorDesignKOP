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
  -> Dashboard/sdtest.py every 30 seconds
  -> local data_YYYY-MM-DD.xlsx and gui_YYYY-MM-DD.xlsx files
  -> Dashboard/popsicle_dashboard.html in the browser
```

The Railway app receives sensor messages, keeps only the configured production
sensors and production-time events, and stores them in PostgreSQL. The local
dashboard folder then reads from PostgreSQL, calculates production metrics, writes
local Excel files, and lets the browser reload the latest dashboard workbook.

## Main Pieces

- `processing.py`: Railway Flask app. It receives iMonnit webhook posts at
  `/imonnit-webhook`, validates the payload, filters by sensor and production
  schedule, creates daily PostgreSQL partitions, avoids duplicate inserts, and
  stores events.
- `main.py` and `Procfile`: Railway startup files. Railway runs the Flask app
  through Gunicorn using `main:app`.
- Railway PostgreSQL: Stores the `events` parent table and daily partition tables
  named like `events_p_20260427`.
- `Dashboard/`: Source of truth for the local dashboard files.
- `Dashboard/server.py`: Starts a local web server on port `8000` and reruns
  `sdtest.py` every 30 seconds.
- `Dashboard/sdtest.py`: Reads new rows from PostgreSQL, updates the local raw
  cache file `data_YYYY-MM-DD.xlsx`, calculates dashboard metrics, and writes
  `gui_YYYY-MM-DD.xlsx`.
- `Dashboard/popsicle_dashboard.html`: The visual dashboard. It reloads the
  current day's `gui_YYYY-MM-DD.xlsx` file every 30 seconds.

## Normal Operating Procedure

Railway should stay deployed and connected to the GitHub repository. When iMonnit
sends data to the Railway webhook, Railway writes accepted events to PostgreSQL.

On the dashboard computer:

1. Open PowerShell in the `Dashboard` folder.
2. Start the local dashboard server:

   ```powershell
   python server.py
   ```

3. Open this page in a browser:

   ```text
   http://localhost:8000/popsicle_dashboard.html
   ```

Leave the PowerShell window open while the dashboard is in use. `server.py` runs
`sdtest.py` immediately, then again every 30 seconds. The browser also reloads
the latest `gui_YYYY-MM-DD.xlsx` workbook every 30 seconds.

## Generated Local Files

- `data_YYYY-MM-DD.xlsx`: Raw local cache of the day's PostgreSQL rows. This lets
  the dashboard processor request only newer rows on later refreshes.
- `gui_YYYY-MM-DD.xlsx`: Dashboard-ready output workbook. The HTML dashboard reads
  this file directly.

These files are created inside `Dashboard/` and are expected to change during
normal use.

## Future Changes

- PostgreSQL retention window: Change `RETAINED_PRODUCTION_DAYS = 8` in
  `processing.py`. This controls how many production days of daily PostgreSQL
  partitions are kept.
- Production schedule: Change `PRODUCTION_START_TIME`, `PRODUCTION_END_TIME`,
  and `PRODUCTION_WEEKDAYS` in `processing.py`.
- Sensors stored in PostgreSQL: Update `DRY_CONTACT_NAMES` and review
  `classify_sensor()` in `processing.py`.
- Dashboard metric logic: Update the sensor filters and output keys in
  `Dashboard/sdtest.py`. For example, the wrap, air-cooled, water-cooled, freezer,
  and fridge calculations are grouped by sensor name there.
- Dashboard display bindings: If `sdtest.py` writes new or renamed workbook keys,
  update the parsing logic in `Dashboard/popsicle_dashboard.html`, especially the
  section that reads values from `gui_YYYY-MM-DD.xlsx`.
- Refresh frequency: Change `REFRESH_SEC` in `Dashboard/server.py` for the Python
  refresh loop and `REFRESH_MS` in `Dashboard/popsicle_dashboard.html` for the
  browser reload interval.

## Troubleshooting

- Dashboard page will not open: Make sure `python server.py` is still running and
  open `http://localhost:8000/popsicle_dashboard.html`.
- Dashboard says the workbook is missing: Run `python server.py` from inside the
  `Dashboard` folder and confirm `gui_YYYY-MM-DD.xlsx` is being created.
- No new data appears: Confirm Railway is deployed, iMonnit is sending webhook
  data, and the current event time falls within the configured production window.
- Database connection error: Confirm the Railway PostgreSQL connection string in
  `Dashboard/sdtest.py` is still current.
- Only old data appears: Delete or archive the current day's local Excel files in
  `Dashboard/`, then restart `server.py` so `sdtest.py` rebuilds them from
  PostgreSQL.

## Developer Notes

The deployed Railway app uses environment variables named `PGHOST`, `PGDATABASE`,
`PGUSER`, `PGPASSWORD`, and `PGPORT` for PostgreSQL. The local dashboard processor
connects from the client computer using the Railway PostgreSQL connection string
configured in `Dashboard/sdtest.py`.

Run these checks after code or documentation changes:

```powershell
python -m py_compile processing.py Dashboard/server.py Dashboard/sdtest.py
python test_processing.py
```
