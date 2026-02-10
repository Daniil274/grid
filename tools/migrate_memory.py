"""
Migration script to convert file-based memory to SQLite.

Converts:
- MEMORY.md → long_term entries
- daily_notes/*.md → short_term entries

Usage:
    python -m tools.migrate_memory [workspace_path]
"""

import re
import sys
import logging
from pathlib import Path
from datetime import datetime
from typing import List, Tuple

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


def parse_memory_md(memory_file: Path) -> List[Tuple[str, str, float]]:
    """
    Parse MEMORY.md into entries.

    Returns:
        List of (content, timestamp, importance) tuples
    """
    if not memory_file.exists():
        logger.warning(f"MEMORY.md not found: {memory_file}")
        return []

    content = memory_file.read_text(encoding='utf-8')
    entries = []

    # Split by ## timestamp headers
    # Format: ## 2024-01-15 14:30:00
    sections = re.split(r'\n## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\n', content)

    # First section is header
    if len(sections) > 1:
        # Process pairs of (timestamp, content)
        for i in range(1, len(sections), 2):
            if i + 1 < len(sections):
                timestamp = sections[i]
                entry_content = sections[i + 1].strip()

                if entry_content:
                    # Assign importance based on keywords (heuristic)
                    importance = 0.5
                    if any(word in entry_content.lower() for word in ['важно', 'critical', 'ключевой', 'необходимо']):
                        importance = 0.9
                    elif any(word in entry_content.lower() for word in ['предпочитает', 'любит', 'нравится', 'preference']):
                        importance = 0.7

                    entries.append((entry_content, timestamp, importance))

    logger.info(f"Parsed {len(entries)} entries from MEMORY.md")
    return entries


def parse_daily_note(note_file: Path) -> List[Tuple[str, str]]:
    """
    Parse a daily note file into entries.

    Returns:
        List of (content, timestamp) tuples
    """
    if not note_file.exists():
        return []

    content = note_file.read_text(encoding='utf-8')
    entries = []

    # Extract date from filename (YYYY-MM-DD.md)
    date_str = note_file.stem

    # Split by ### timestamp headers
    # Format: ### 14:30:00
    sections = re.split(r'\n### (\d{2}:\d{2}:\d{2})\n', content)

    # First section is header
    if len(sections) > 1:
        # Process pairs of (time, content)
        for i in range(1, len(sections), 2):
            if i + 1 < len(sections):
                time_str = sections[i]
                entry_content = sections[i + 1].strip()

                if entry_content:
                    # Combine date + time
                    timestamp = f"{date_str} {time_str}"
                    entries.append((entry_content, timestamp))

    return entries


def migrate_workspace(workspace: Path, db_path: Path):
    """
    Migrate a single workspace from files to SQLite.

    Args:
        workspace: Path to workspace directory
        db_path: Path to SQLite database file
    """
    from core.memory_store import MemoryStore

    logger.info(f"Migrating workspace: {workspace}")
    logger.info(f"Database: {db_path}")

    # Initialize MemoryStore
    store = MemoryStore(db_path=str(db_path))

    # Migrate MEMORY.md
    memory_file = workspace / "MEMORY.md"
    long_term_entries = parse_memory_md(memory_file)

    for content, timestamp, importance in long_term_entries:
        try:
            # Convert timestamp to ISO format
            dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
            iso_timestamp = dt.isoformat()

            # Save to SQLite
            entry_id = store.save(
                content=content,
                type="long_term",
                importance=importance
            )

            # Update created_at manually
            with store._get_connection() as conn:
                conn.execute(
                    "UPDATE memory SET created_at = ?, updated_at = ? WHERE id = ?",
                    (iso_timestamp, iso_timestamp, entry_id)
                )
                conn.commit()

            logger.debug(f"Migrated long-term entry #{entry_id}: {content[:50]}...")

        except Exception as e:
            logger.error(f"Failed to migrate entry: {e}")

    logger.info(f"✅ Migrated {len(long_term_entries)} long-term entries")

    # Migrate daily notes
    daily_notes_dir = workspace / "daily_notes"
    if daily_notes_dir.exists():
        note_files = sorted(daily_notes_dir.glob("*.md"))
        total_short_term = 0

        for note_file in note_files:
            entries = parse_daily_note(note_file)

            for content, timestamp in entries:
                try:
                    # Convert timestamp to ISO format
                    dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
                    iso_timestamp = dt.isoformat()

                    # Save to SQLite
                    entry_id = store.save(
                        content=content,
                        type="short_term",
                        importance=0.3
                    )

                    # Update created_at manually
                    with store._get_connection() as conn:
                        conn.execute(
                            "UPDATE memory SET created_at = ?, updated_at = ? WHERE id = ?",
                            (iso_timestamp, iso_timestamp, entry_id)
                        )
                        conn.commit()

                    logger.debug(f"Migrated short-term entry #{entry_id} from {note_file.name}")
                    total_short_term += 1

                except Exception as e:
                    logger.error(f"Failed to migrate daily note entry: {e}")

        logger.info(f"✅ Migrated {total_short_term} short-term entries from {len(note_files)} daily notes")

    # Show final stats
    stats = store.get_stats()
    logger.info(f"\n📊 Final stats:")
    logger.info(f"  Total entries: {stats['total_entries']}")
    logger.info(f"  By type: {stats['by_type']}")
    logger.info(f"  Database size: {stats['db_size_mb']:.2f} MB")


def main():
    """Main migration function."""
    import argparse

    parser = argparse.ArgumentParser(description="Migrate file-based memory to SQLite")
    parser.add_argument(
        "workspace",
        nargs="?",
        default="data",
        help="Workspace directory (default: data)"
    )
    parser.add_argument(
        "--db-path",
        help="Custom database path (default: workspace/memory.db)"
    )
    parser.add_argument(
        "--all-users",
        action="store_true",
        help="Migrate all user workspaces in data/"
    )

    args = parser.parse_args()

    if args.all_users:
        # Migrate all user_* directories
        data_dir = Path("data")
        if not data_dir.exists():
            logger.error(f"Data directory not found: {data_dir}")
            return 1

        user_dirs = [d for d in data_dir.iterdir() if d.is_dir() and d.name.startswith("user_")]

        if not user_dirs:
            logger.warning("No user directories found in data/")
            return 0

        logger.info(f"Found {len(user_dirs)} user workspace(s) to migrate")

        for user_dir in user_dirs:
            workspace = user_dir
            db_path = user_dir / "memory.db"

            logger.info(f"\n{'='*60}")
            migrate_workspace(workspace, db_path)

        logger.info(f"\n{'='*60}")
        logger.info(f"✅ Migration complete for {len(user_dirs)} workspace(s)")

    else:
        # Migrate single workspace
        workspace = Path(args.workspace)
        if not workspace.exists():
            logger.error(f"Workspace not found: {workspace}")
            return 1

        db_path = Path(args.db_path) if args.db_path else workspace / "memory.db"

        migrate_workspace(workspace, db_path)
        logger.info("✅ Migration complete")

    return 0


if __name__ == "__main__":
    sys.exit(main())
