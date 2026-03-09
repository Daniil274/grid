"""
Простой HTTP‑сервер для просмотра памяти (контекст + SQLite) и beads.

Запуск:
    python serve_memory_dashboard.py [--port 8788]

Открывает memory_dashboard.html; API:
  GET /api/users     — список user_id (по каталогам workspace/user_* и data/user_*)
  GET /api/context?user_id=XXX — контекст из data/user_XXX/context.json
  GET /api/memory?user_id=XXX  — записи из workspace/user_XXX/memory.db (long_term, short_term, task, ...)
  GET /api/beads?user_id=XXX   — beads: ready + полный список из .beads/issues.jsonl с иерархией
  GET /api/timeline?user_id=XXX — единая хронология: сообщения + запуски агентов + события beads
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

# Корень проекта = директория скрипта
ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT 
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
        # Полная история выполнений для дашборда и хронологии
        execution_list = []
        for e in execs:
            execution_list.append({
                "agent_name": e.get("agent_name"),
                "input_message": e.get("input_message"),
                "output": e.get("output"),
                "error": e.get("error"),
                "start_time": e.get("start_time"),
                "end_time": e.get("end_time"),
                "context_id": e.get("context_id"),
                "tools_used": e.get("tools_used") or [],
                "token_usage": e.get("token_usage"),
            })
        out["contexts"][cid] = {
            "conversation": conv,
            "execution_count": len(execs),
            "execution_history": execution_list,
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


def _load_beads_from_jsonl(cwd: Path):
    """Прочитать все beads из .beads/issues.jsonl. Возвращает список dict и граф зависимостей."""
    issues_path = cwd / ".beads" / "issues.jsonl"
    if not issues_path.exists():
        return [], {}
    items = []
    by_id = {}
    with open(issues_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                bid = obj.get("id") or ""
                items.append(obj)
                by_id[bid] = obj
            except json.JSONDecodeError:
                continue
    # Иерархия: для каждого id собираем children (кто от него зависит) и parents (от кого зависит)
    # В beads: dependencies[] = { issue_id, depends_on_id, type } — issue_id блокируется depends_on_id
    # То есть child depends_on parent → parent блокирует child → в дереве parent выше, child ниже
    children = {}
    parents = {}
    for b in items:
        bid = b.get("id")
        if not bid:
            continue
        deps = b.get("dependencies") or []
        for d in deps:
            child_id = d.get("issue_id")
            parent_id = d.get("depends_on_id")
            if child_id and parent_id:
                children.setdefault(parent_id, []).append(child_id)
                parents.setdefault(child_id, []).append(parent_id)
    hierarchy = {"by_id": by_id, "children": children, "parents": parents, "roots": [i["id"] for i in items if i.get("id") and not parents.get(i["id"])]}
    return items, hierarchy


def load_beads(user_id: str):
    """Beads: ready (bd) + полный список из .beads/issues.jsonl с notes, description и иерархией зависимостей."""
    cwd = WORKSPACE / f"user_{user_id}"
    if not cwd.is_dir():
        return {"error": "user workspace not found", "path": str(cwd)}

    # 1) Полный список из issues.jsonl с иерархией
    items, hierarchy = _load_beads_from_jsonl(cwd)
    # Сортировка по updated_at (новые сверху)
    def sort_key(b):
        t = b.get("updated_at") or b.get("created_at") or ""
        return (t, b.get("id") or "")
    items_sorted = sorted(items, key=sort_key, reverse=True)

    # 2) Готовые к работе — через bd ready (если доступен)
    bd_path = _find_bd()
    ready_data = None
    err = None
    try:
        r = subprocess.run(
            [bd_path, "ready", "--json"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        raw = (r.stdout or "").strip()
        if r.returncode == 0 and raw:
            try:
                ready_data = json.loads(raw)
            except json.JSONDecodeError:
                ready_data = {"_raw": raw[:500]}
        elif r.returncode != 0 and not err:
            err = (r.stderr or raw or "bd failed")[:500]
    except FileNotFoundError:
        err = "bd CLI not found. Set BEADS_BD_PATH or install beads."
    except subprocess.TimeoutExpired:
        err = "bd command timed out"
    except Exception as e:
        err = str(e)[:500]

    return {
        "ready": ready_data,
        "list": items_sorted,
        "hierarchy": hierarchy,
        "error": err,
    }


def _timeline_ts(ev):
    """Нормализовать время события в float (Unix) для сортировки."""
    ts = ev.get("_ts") or ev.get("timestamp") or ev.get("updated_at") or ev.get("created_at") or ev.get("start_time")
    if ts is None:
        return 0.0
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0.0
    return 0.0


def _timeline_sort_key(ev):
    """Ключ сортировки: сначала по времени (новые первые), затем по типу."""
    return (_timeline_ts(ev), ev.get("_order", 0))


def load_timeline(user_id: str, limit: int = 500):
    """Единая хронология: сообщения диалога + запуски агентов (execution_history) + события beads."""
    events = []

    # 1) Контекст: сообщения и выполнения
    ctx = load_context(user_id)
    if not ctx.get("error") and ctx.get("contexts"):
        for cid, bucket in ctx["contexts"].items():
            for msg in bucket.get("conversation", []):
                ts = msg.get("timestamp") or bucket.get("updated_at") or ""
                events.append({
                    "type": "message",
                    "context_id": cid,
                    "role": msg.get("role"),
                    "content": (msg.get("content") or "")[:2000],
                    "timestamp": ts,
                    "_ts": ts,
                    "_order": 0,
                })
            for ex in bucket.get("execution_history", []):
                st = ex.get("start_time")
                events.append({
                    "type": "execution",
                    "context_id": cid,
                    "agent_name": ex.get("agent_name"),
                    "input_message": (ex.get("input_message") or "")[:500],
                    "output": (ex.get("output") or "")[:1500],
                    "error": ex.get("error"),
                    "start_time": st,
                    "end_time": ex.get("end_time"),
                    "tools_used": ex.get("tools_used") or [],
                    "_ts": st if isinstance(st, (int, float)) else None,
                    "timestamp": str(st) if st is not None else None,
                    "_order": 1,
                })

    # 2) Beads: каждую задачу как событие по updated_at
    beads = load_beads(user_id)
    if not beads.get("error") and beads.get("list"):
        for b in beads["list"]:
            ts = b.get("updated_at") or b.get("created_at") or ""
            events.append({
                "type": "bead",
                "id": b.get("id"),
                "title": b.get("title"),
                "description": (b.get("description") or "")[:500],
                "notes": (b.get("notes") or "")[:1500],
                "status": b.get("status"),
                "priority": b.get("priority"),
                "issue_type": b.get("issue_type"),
                "created_at": b.get("created_at"),
                "updated_at": ts,
                "closed_at": b.get("closed_at"),
                "dependencies": b.get("dependencies") or [],
                "_ts": ts,
                "timestamp": ts,
                "_order": 2,
            })

    # Сортировка: сначала по _ts (чем позже — тем выше при reverse), затем по _order
    events.sort(key=_timeline_sort_key, reverse=True)
    if limit > 0:
        events = events[:limit]
    return {"events": events}


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
        if path == "/api/timeline":
            user_id = (qs.get("user_id") or [None])[0]
            if not user_id:
                self.send_json({"error": "user_id required"}, status=400)
                return
            limit = int((qs.get("limit") or ["500"])[0])
            self.send_json(load_timeline(user_id, limit=limit))
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
    print("API: /api/users, /api/context, /api/memory, /api/beads, /api/timeline (user_id=XXX)")
    print("Ctrl+C to stop.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
