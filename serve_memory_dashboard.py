"""
Простой HTTP‑сервер для просмотра памяти (контекст + SQLite) и beads.

Запуск:
    python serve_memory_dashboard.py [--port 8788]

Открывает memory_dashboard.html; API:
  GET /api/users     — список user_id (по каталогам workspace/user_* и data/user_*)
  GET /api/context?user_id=XXX — контекст из data/user_XXX/context.json
  GET /api/memory?user_id=XXX  — записи из workspace/user_XXX/memory.db (long_term, short_term, task, ...)
  GET /api/beads?user_id=XXX   — beads: ready + list из workspace/user_XXX (bd ready --json, bd list --json)
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Корень проекта = директория скрипта
ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT / "workspace"
PERSIST = ROOT / "data"


def get_users():
    """Список user_id: из workspace/user_* и data/user_*."""
    users = set()
    for d in (WORKSPACE, PERSIST):
        if not d.exists():
            continue
        for p in d.iterdir():
            if p.is_dir() and p.name.startswith("user_") and p.name[5:].isdigit():
                users.add(p.name[5:])
    return sorted(users, key=int)


def load_context(user_id: str):
    """Загрузить context.json для пользователя. Без зависимостей от core."""
    path = PERSIST / f"user_{user_id}" / "context.json"
    if not path.exists():
        return {"error": "context.json not found", "path": str(path)}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {"error": str(e)}
    # Упрощённый вид: активный контекст и все диалоги
    active_id = data.get("active_context_id")
    contexts = data.get("contexts", {})
    out = {
        "active_context_id": active_id,
        "context_ids": list(contexts.keys()),
        "contexts": {},
    }
    for cid, bucket in contexts.items():
        conv = bucket.get("conversation_history", [])
        execs = bucket.get("execution_history", [])
        out["contexts"][cid] = {
            "conversation": conv,
            "execution_count": len(execs),
            "executions_preview": [
                {"agent": e.get("agent_name"), "input": (e.get("input_message") or "")[:80]}
                for e in execs[-5:]
            ],
            "created_at": bucket.get("created_at"),
            "updated_at": bucket.get("updated_at"),
        }
    return out


def load_memory(user_id: str, limit: int = 200):
    """Прочитать записи из SQLite memory.db пользователя (только чтение)."""
    db_path = WORKSPACE / f"user_{user_id}" / "memory.db"
    if not db_path.exists():
        return {"error": "memory.db not found", "path": str(db_path)}
    try:
        conn = sqlite3.connect(str(db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT id, type, content, tags, created_at, updated_at, session_id, task_id, user_id, agent_id, status, importance, is_archived FROM memory ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return {"entries": rows}
    except Exception as e:
        return {"error": str(e)}


def _find_bd():
    """Путь к bd CLI: BEADS_BD_PATH, ~/.local/bin/bd, или bd из PATH."""
    path = os.environ.get("BEADS_BD_PATH")
    if path and os.path.isfile(path):
        return path
    home_bd = Path.home() / ".local" / "bin" / "bd"
    if home_bd.exists():
        return str(home_bd)
    return shutil.which("bd") or "bd"


def load_beads(user_id: str):
    """Вызвать bd ready --json и bd list --json в workspace/user_{user_id}. Без зависимостей от core."""
    cwd = WORKSPACE / f"user_{user_id}"
    if not cwd.is_dir():
        return {"error": "user workspace not found", "path": str(cwd)}
    bd_path = _find_bd()
    out = {"ready": None, "list": None, "error": None}

    for cmd_name, args in [("ready", ["ready", "--json"]), ("list", ["list", "--json"])]:
        try:
            r = subprocess.run(
                [bd_path] + args,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
            )
            raw = (r.stdout or "").strip()
            if r.returncode != 0:
                if out["error"] is None:
                    out["error"] = (r.stderr or raw or "bd failed")[:500]
                continue
            if raw:
                try:
                    data = json.loads(raw)
                    out[cmd_name] = data
                except json.JSONDecodeError:
                    out[cmd_name] = {"_raw": raw[:2000]}
        except FileNotFoundError:
            out["error"] = "bd CLI not found. Set BEADS_BD_PATH or install beads."
            break
        except subprocess.TimeoutExpired:
            out["error"] = "bd command timed out"
            break
        except Exception as e:
            out["error"] = str(e)[:500]
            break
    return out


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        qs = parse_qs(parsed.query)

        if path == "/api/users":
            self.send_json(get_users())
            return
        if path == "/api/context":
            user_id = (qs.get("user_id") or [None])[0]
            if not user_id:
                self.send_json({"error": "user_id required"}, status=400)
                return
            self.send_json(load_context(user_id))
            return
        if path == "/api/memory":
            user_id = (qs.get("user_id") or [None])[0]
            if not user_id:
                self.send_json({"error": "user_id required"}, status=400)
                return
            limit = int((qs.get("limit") or ["200"])[0])
            self.send_json(load_memory(user_id, limit=limit))
            return
        if path == "/api/beads":
            user_id = (qs.get("user_id") or [None])[0]
            if not user_id:
                self.send_json({"error": "user_id required"}, status=400)
                return
            self.send_json(load_beads(user_id))
            return

        # Любой не-API путь — отдаём дашборд
        file_path = ROOT / "memory_dashboard.html"
        if not file_path.exists():
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        with open(file_path, "rb") as f:
            self.wfile.write(f.read())

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format, *args):
        print(format % args)


def main():
    parser = argparse.ArgumentParser(description="Serve Memory Dashboard")
    parser.add_argument("--port", type=int, default=8788, help="Port (default 8788)")
    args = parser.parse_args()
    os.chdir(ROOT)
    server = HTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/memory_dashboard.html"
    print(f"Memory Dashboard: {url}")
    print("API: /api/users, /api/context?user_id=XXX, /api/memory?user_id=XXX, /api/beads?user_id=XXX")
    print("Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
