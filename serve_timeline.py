"""
Запуск сервера визуализации агентов.
python serve_timeline.py [--port 8789] [--db data/timeline.db]
Открывает timeline_dashboard.html по адресу http://127.0.0.1:8789/
"""

import argparse
import logging
import sys
from pathlib import Path

# Снижаем шум asyncio при принудительном закрытии соединения (WinError 10054)
logging.getLogger("asyncio").setLevel(logging.CRITICAL)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    parser = argparse.ArgumentParser(description="Agent Timeline Dashboard")
    parser.add_argument("--port", type=int, default=8789, help="Port (default 8789)")
    parser.add_argument("--db", default=str(ROOT / "data" / "timeline.db"), help="SQLite DB path")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("uvicorn not installed. Run: pip install uvicorn")
        sys.exit(1)

    from timeline.server import create_app

    app = create_app(db_path=args.db)

    url = f"http://{args.host}:{args.port}/"
    print(f"Agent Timeline Dashboard: {url}")
    print(f"DB: {args.db}")
    print("Ctrl+C to stop.\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
