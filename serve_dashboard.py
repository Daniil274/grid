"""
Tiny HTTP server to serve SI Dashboard.

Usage:
    python serve_dashboard.py [--port 8787]

Opens si_dashboard.html in the default browser with auto-loading
of logs/blackboard.json and logs/pipeline_memory.json.
"""

import argparse
import http.server
import os
import threading
import webbrowser


def main():
    parser = argparse.ArgumentParser(description="Serve SI Dashboard")
    parser.add_argument("--port", type=int, default=8787, help="Port (default 8787)")
    args = parser.parse_args()

    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    handler = http.server.SimpleHTTPRequestHandler
    handler.extensions_map.update({".json": "application/json"})
    server = http.server.HTTPServer(("127.0.0.1", args.port), handler)

    url = f"http://127.0.0.1:{args.port}/si_dashboard.html"
    print(f"SI Dashboard: {url}")
    print("Press Ctrl+C to stop.\n")

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
