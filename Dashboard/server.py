"""Local server for the King of Pops dashboard.

Run this file from the Dashboard folder with `python server.py`, then open
http://localhost:8000/popsicle_dashboard.html. The server hosts the dashboard
files and runs sdtest.py every 30 seconds so the local Excel dashboard workbook
stays fresh.
"""

import http.server
import os
import socketserver
import subprocess
import threading
import time

PORT = 8000
REFRESH_SEC = 30
SDTEST = "sdtest.py"

os.chdir(os.path.dirname(os.path.abspath(__file__)))


def run_refresh():
    """Run sdtest.py once and print enough output for operator troubleshooting."""
    result = subprocess.run(
        ["python", SDTEST],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.stdout:
        print(f"  [sdtest] {result.stdout.strip()}")
    if result.returncode != 0 and result.stderr:
        print(f"  [sdtest ERROR] {result.stderr.strip()[:500]}")
    return result.returncode


def refresh_loop():
    """Run the dashboard data refresh script forever in the background."""
    while True:
        try:
            run_refresh()
        except Exception as e:
            print(f"  [sdtest ERROR] {e}")
        time.sleep(REFRESH_SEC)


# Run sdtest.py once immediately so the browser has a fresh workbook to load.
print()
print("  King of Pops Dashboard")
print("  -------------------------------------")
print("  Running initial data refresh...")
try:
    run_refresh()
    print("  Initial refresh complete.")
except Exception as e:
    print(f"  [sdtest ERROR] {e}")

# Start a daemon thread so the web server can respond while data refreshes.
t = threading.Thread(target=refresh_loop, daemon=True)
t.start()

class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    """Serve dashboard files without browser caching during live operation."""

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


handler = NoCacheHandler

with socketserver.TCPServer(("", PORT), handler) as httpd:
    print()
    print(f"  Server running at http://localhost:{PORT}")
    print(f"  Open -> http://localhost:{PORT}/popsicle_dashboard.html")
    print()
    print(f"  Data refreshes every {REFRESH_SEC}s via {SDTEST}")
    print("  To stop: press Ctrl+C")
    print()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
