"""
Claude Chat History Tools
Provides semantic/FTS search over exported Claude.ai conversation history.

Index is built lazily on first call and cached at data/claude_history.db.
Source path configured via env var CLAUDE_HISTORY_PATH or auto-detected.
"""

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from agents import RunContextWrapper, function_tool

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────

def _data_dir() -> Path:
    """Resolve data/ directory relative to this file."""
    return Path(__file__).parent.parent / "data"


def _index_path() -> Path:
    return _data_dir() / "claude_history.db"


def _source_path() -> Optional[Path]:
    """
    Resolve conversations.json.
    Priority:
      1. CLAUDE_HISTORY_PATH env var
      2. workspace/data-*/conversations.json (newest batch)
    """
    env = os.environ.get("CLAUDE_HISTORY_PATH")
    if env:
        p = Path(env)
        if p.exists():
            return p
        logger.warning(f"CLAUDE_HISTORY_PATH set but file not found: {env}")

    workspace = Path(__file__).parent.parent / "workspace"
    if workspace.exists():
        candidates = sorted(
            workspace.glob("data-*/conversations.json"),
            key=lambda p: p.parent.name,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


# ──────────────────────────────────────────────────────────────
# Index builder
# ──────────────────────────────────────────────────────────────

_INDEX_BUILT = False  # in-process flag to avoid repeated checks


def _ensure_index(force: bool = False) -> bool:
    """
    Build the FTS5 index if missing or stale. Returns True on success.
    Thread-unsafe but acceptable for single-process use.
    """
    global _INDEX_BUILT

    if _INDEX_BUILT and not force:
        return True

    src = _source_path()
    if src is None:
        logger.error("claude_history: conversations.json not found. Set CLAUDE_HISTORY_PATH.")
        return False

    idx = _index_path()
    idx.parent.mkdir(parents=True, exist_ok=True)

    # Skip rebuild if index is newer than source
    if not force and idx.exists() and idx.stat().st_mtime >= src.stat().st_mtime:
        _INDEX_BUILT = True
        return True

    logger.info(f"claude_history: building FTS index from {src} …")
    t0 = time.time()

    try:
        with open(src, "r", encoding="utf-8") as f:
            conversations = json.load(f)
    except Exception as e:
        logger.error(f"claude_history: failed to load source: {e}")
        return False

    con = sqlite3.connect(str(idx))
    cur = con.cursor()

    cur.executescript("""
        DROP TABLE IF EXISTS conversations;
        DROP TABLE IF EXISTS messages;

        CREATE TABLE conversations (
            uuid        TEXT PRIMARY KEY,
            name        TEXT,
            created_at  TEXT,
            updated_at  TEXT,
            msg_count   INTEGER DEFAULT 0
        );

        CREATE VIRTUAL TABLE messages USING fts5(
            conv_uuid   UNINDEXED,
            conv_name,
            sender      UNINDEXED,
            text,
            created_at  UNINDEXED,
            tokenize    = "unicode61 remove_diacritics 1"
        );

        CREATE TABLE message_rows (
            rowid       INTEGER PRIMARY KEY AUTOINCREMENT,
            conv_uuid   TEXT,
            msg_uuid    TEXT,
            sender      TEXT,
            text        TEXT,
            created_at  TEXT,
            position    INTEGER
        );
    """)

    conv_rows = []
    msg_rows = []
    fts_rows = []

    for conv in conversations:
        uuid = conv.get("uuid", "")
        name = conv.get("name") or ""
        created_at = conv.get("created_at", "")
        updated_at = conv.get("updated_at", "")
        msgs = conv.get("chat_messages") or []
        if not msgs:
            continue

        conv_rows.append((uuid, name, created_at, updated_at, len(msgs)))

        for pos, msg in enumerate(msgs):
            sender = msg.get("sender", "assistant")
            text = msg.get("text", "")
            if not text and msg.get("content"):
                text = " ".join(c.get("text", "") for c in msg["content"] if c.get("text"))
            if not text:
                continue
            ts = msg.get("created_at") or (msg.get("content") or [{}])[0].get("start_timestamp", "")
            muuid = msg.get("uuid", "")

            msg_rows.append((uuid, muuid, sender, text, ts, pos))
            fts_rows.append((uuid, name, sender, text, ts))

    cur.executemany(
        "INSERT OR REPLACE INTO conversations VALUES (?,?,?,?,?)", conv_rows
    )
    cur.executemany(
        "INSERT INTO message_rows (conv_uuid,msg_uuid,sender,text,created_at,position) VALUES (?,?,?,?,?,?)",
        msg_rows,
    )
    cur.executemany(
        "INSERT INTO messages (conv_uuid,conv_name,sender,text,created_at) VALUES (?,?,?,?,?)",
        fts_rows,
    )

    con.commit()
    con.close()

    elapsed = time.time() - t0
    logger.info(
        f"claude_history: indexed {len(conv_rows)} conversations, "
        f"{len(msg_rows)} messages in {elapsed:.1f}s → {idx}"
    )
    _INDEX_BUILT = True
    return True


# ──────────────────────────────────────────────────────────────
# Search helper
# ──────────────────────────────────────────────────────────────

def _search(query: str, limit: int = 5, sender_filter: Optional[str] = None) -> dict:
    """
    Full-text search over message history.
    Returns conversation pairs (human + assistant) surrounding each match.
    """
    if not _ensure_index():
        return {"error": "Index not available. Set CLAUDE_HISTORY_PATH.", "results": []}

    idx = _index_path()
    con = sqlite3.connect(f"file:{idx}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    # Step 1: FTS search — get conv_uuid, conv_name, rank, snippet
    # Don't JOIN here; FTS rowid ≠ message_rows rowid reliably
    try:
        cur.execute(
            """
            SELECT conv_uuid, conv_name,
                   snippet(messages, 3, '[', ']', '…', 24) AS snip,
                   created_at,
                   rank
            FROM messages
            WHERE messages MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (query, limit * 6),  # over-fetch to allow dedup by conversation
        )
        fts_rows = cur.fetchall()
    except sqlite3.OperationalError as e:
        con.close()
        return {"error": f"FTS error: {e}", "results": []}

    # Step 2: Deduplicate by conversation (keep best-rank hit per conv)
    seen_convs: dict = {}
    for row in fts_rows:
        cid = row["conv_uuid"]
        if cid not in seen_convs:
            seen_convs[cid] = row
        if len(seen_convs) >= limit:
            break

    # Step 3: For each matched conversation, find the best-matching message
    # position and fetch a context window around it
    results = []
    for cid, best_fts in seen_convs.items():
        snip_text = best_fts["snip"] or ""

        # Find position of the matching message in message_rows
        # Match by looking for the snippet content (strip markers)
        clean_snip = snip_text.replace("[", "").replace("]", "").replace("…", "")[:60]

        pos = 0
        if clean_snip.strip():
            cur.execute(
                """
                SELECT position FROM message_rows
                WHERE conv_uuid = ? AND text LIKE ?
                ORDER BY position LIMIT 1
                """,
                (cid, f"%{clean_snip.strip()[:40]}%"),
            )
            row = cur.fetchone()
            if row:
                pos = row["position"]

        # Fetch context window: 1 before + 3 after the match
        cur.execute(
            """
            SELECT sender, text, created_at, position
            FROM message_rows
            WHERE conv_uuid = ?
              AND position BETWEEN ? AND ?
            ORDER BY position
            """,
            (cid, max(0, pos - 1), pos + 3),
        )
        window = [dict(r) for r in cur.fetchall()]

        # Trim long messages
        for msg in window:
            if len(msg["text"]) > 800:
                msg["text"] = msg["text"][:800] + "…"

        results.append(
            {
                "conversation": best_fts["conv_name"] or "(unnamed)",
                "date": (best_fts["created_at"] or "")[:10],
                "match_snippet": snip_text,
                "messages": window,
            }
        )

    con.close()
    return {"results": results, "total_matched": len(fts_rows), "query": query}


# ──────────────────────────────────────────────────────────────
# Agent-facing tool
# ──────────────────────────────────────────────────────────────

@function_tool
async def claude_history_search(
    context: RunContextWrapper,
    query: str,
    limit: int = 5,
) -> str:
    """
    Search through personal Claude.ai chat history using full-text search.

    Use this to recall past discussions, solutions, decisions, or context that
    the user has previously worked through with Claude. Returns conversation
    excerpts (question + answer pairs) most relevant to the query.

    Args:
        query: Natural language search query (e.g. "ALSA biquad filters", "SSH tunnel config")
        limit: Number of conversations to return (1-20, default 5)

    Returns:
        JSON string with matched conversation excerpts.
        Each result contains: conversation name, date, matching messages.
    """
    limit = max(1, min(20, limit))

    result = _search(query, limit=limit)

    if "error" in result:
        return f"[claude_history_search] Error: {result['error']}"

    results = result.get("results", [])
    if not results:
        return f"[claude_history_search] No results found for query: '{query}'"

    lines = [f"## Claude conversation history: '{query}'\n"]
    lines.append(f"Matches found: {result.get('total_matched', 0)}, displayed: {len(results)}\n")

    for i, r in enumerate(results, 1):
        lines.append(f"### {i}. {r['conversation']}  ({r['date']})")
        lines.append(f"> Snippet: {r['match_snippet']}\n")
        for msg in r["messages"]:
            role = "**You**" if msg["sender"] == "human" else "**Claude**"
            lines.append(f"{role}: {msg['text']}\n")
        lines.append("---")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# Tool registry
# ──────────────────────────────────────────────────────────────

HISTORY_TOOLS = {
    "claude_history_search": claude_history_search,
}
