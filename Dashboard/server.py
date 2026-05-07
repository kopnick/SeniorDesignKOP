"""
King of Pops Dashboard Local Server
-----------------------------------
1. Put this file in the same folder as popsicle_dashboard.html and sdtest.py
2. Run:  python server.py
3. Open the localhost URL printed by the server.

The server automatically runs sdtest.py every few seconds to keep the
dashboard data fresh. The browser will pick up changes on its next
auto-refresh cycle.
"""

import http.server
import os
import socketserver
import subprocess
import sys
import threading
import time


PORT = 8000
REFRESH_SEC = 5
SDTEST = "sdtest.py"
PORT_SCAN_LIMIT = 20


os.chdir(os.path.dirname(os.path.abspath(__file__)))


def run_sdtest(label):
    try:
        result = subprocess.run(
            [sys.executable, SDTEST],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as e:
        print(f"  [sdtest ERROR] {label} failed: {e}")
        return False

    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip())

    if result.returncode:
        print(f"  [sdtest ERROR] {label} exited with code {result.returncode}")
        return False

    return True


def refresh_loop():
    while True:
        time.sleep(REFRESH_SEC)
        run_sdtest("background refresh")


print()
print("  King of Pops Dashboard")
print("  ------------------------------------")
print("  Running initial data refresh...")
if run_sdtest("initial refresh"):
    print("  Initial refresh complete.")
else:
    print("  Initial refresh failed; serving dashboard with any existing local files.")


t = threading.Thread(target=refresh_loop, daemon=True)
t.start()

handler = http.server.SimpleHTTPRequestHandler


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def create_server(start_port):
    for port in range(start_port, start_port + PORT_SCAN_LIMIT):
        try:
            return port, ReusableTCPServer(("", port), handler)
        except OSError as exc:
            if getattr(exc, "winerror", None) in (10013, 10048) or getattr(exc, "errno", None) == 98:
                print(f"  Port {port} is unavailable; trying {port + 1}...")
                continue
            raise
    raise OSError(
        f"Could not start server: ports {start_port}-{start_port + PORT_SCAN_LIMIT - 1} are in use."
    )


port, httpd = create_server(PORT)

with httpd:
    print()
    print(f"  Server running at http://localhost:{port}")
    print(f"  Open -> http://localhost:{port}/popsicle_dashboard.html")
    print()
    print(f"  Data refreshes every {REFRESH_SEC}s via {SDTEST}")
    print("  To stop: press Ctrl+C")
    print()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
