"""
Post-compact cleanup and context restoration for Grid.

Mirrors Claude Code's runPostCompactCleanup() and the file/skill restoration
logic in compactConversation():

After compaction:
1. resetMicrocompactState() — the time-based MC has no persistent state in
   Grid, but this call is preserved for symmetry and future use.
2. Clear getUserContext cache — forces CLAUDE.md re-read on next turn.
3. Clear session messages cache if applicable.
4. Restore recently-read files (within token budget) as attachments.
5. Restore invoked skills (within token budget).

The file/skill restoration state is stored in PostCompactState, which is
per-session and should be passed or stored at the agent level in production.
For now, a module-level instance is used for simplicity, with reset available.
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .micro_compact import reset_microcompact_state
from .utils import rough_token_count

logger = logging.getLogger("compact.post")


# Token budgets (mirrors CC's POST_COMPACT_* constants)
POST_COMPACT_MAX_FILES_TO_RESTORE = 5
POST_COMPACT_TOKEN_BUDGET = 50_000
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
POST_COMPACT_MAX_TOKENS_PER_SKILL = 5_000
POST_COMPACT_SKILLS_TOKEN_BUDGET = 25_000


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

@dataclass
class FileRestoreInfo:
    path: str
    content: str
    timestamp: datetime
    token_count: int = 0


@dataclass
class SkillRestoreInfo:
    name: str
    path: str
    content: str
    invoked_at: datetime
    token_count: int = 0


@dataclass
class PostCompactState:
    """
    Tracks files and skills read during a session for post-compact restoration.
    Mirrors CC's fileStateCache (readFileState) and invoked skills tracking.
    """
    # path -> (content, timestamp)
    file_state: Dict[str, Tuple[str, datetime]] = field(default_factory=dict)
    # name -> SkillRestoreInfo
    invoked_skills: Dict[str, SkillRestoreInfo] = field(default_factory=dict)


_state: PostCompactState = PostCompactState()


def get_post_compact_state() -> PostCompactState:
    return _state


def reset_post_compact_state() -> None:
    global _state
    _state = PostCompactState()


# ---------------------------------------------------------------------------
# Recording API (call these from tools/skill invocations)
# ---------------------------------------------------------------------------

def record_file_read(path: str, content: str) -> None:
    """Record a file read so it can be restored after compaction."""
    _state.file_state[path] = (content, datetime.now())


def record_skill_invocation(name: str, path: str, content: str) -> None:
    """Record a skill invocation for post-compact re-injection."""
    token_count = max(1, len(content) // 4)
    _state.invoked_skills[name] = SkillRestoreInfo(
        name=name,
        path=path,
        content=content,
        invoked_at=datetime.now(),
        token_count=token_count,
    )


# ---------------------------------------------------------------------------
# Post-compact cleanup
# ---------------------------------------------------------------------------

def run_post_compact_cleanup(context_id: Optional[str] = None) -> None:
    """
    Run cleanup after any compaction (auto, manual, or reactive).

    Mirrors CC's runPostCompactCleanup():
    - resetMicrocompactState() — no-op for time-based MC, kept for symmetry.
    - Clears any caches that hold state invalidated by compaction.
    - Does NOT clear invoked skill content (skills survive across compactions
      so createSkillAttachmentIfNeeded can include them in subsequent compaction).

    Args:
        context_id: Optional context ID for logging.
    """
    # 1. Reset microcompact state (no-op for time-based, kept for symmetry)
    reset_microcompact_state()

    # 2. Clear recently-read file cache (forces re-read next turn if needed)
    #    In CC: context.readFileState.clear() and context.loadedNestedMemoryPaths.clear()
    #    In Grid: clear file_state so the next compaction starts fresh
    _state.file_state.clear()

    logger.info(
        f"Post-compact cleanup complete"
        + (f" (context_id={context_id})" if context_id else "")
    )


# ---------------------------------------------------------------------------
# Restoration helpers (used by compact_conversation.py)
# ---------------------------------------------------------------------------

def create_post_compact_file_attachments(
    max_files: int = POST_COMPACT_MAX_FILES_TO_RESTORE,
    max_tokens_per_file: int = POST_COMPACT_MAX_TOKENS_PER_FILE,
    token_budget: int = POST_COMPACT_TOKEN_BUDGET,
) -> List[FileRestoreInfo]:
    """
    Return the most recently read files that fit within the token budget.
    Mirrors CC's createPostCompactFileAttachments().
    """
    # Sort by recency (most recent first)
    sorted_files = sorted(
        _state.file_state.items(),
        key=lambda kv: kv[1][1],  # timestamp
        reverse=True,
    )[:max_files]

    results: List[FileRestoreInfo] = []
    used = 0

    for path, (content, ts) in sorted_files:
        token_count = max(1, len(content) // 4)

        if token_count > max_tokens_per_file:
            content = content[: max_tokens_per_file * 4]
            token_count = max_tokens_per_file

        if used + token_count > token_budget:
            break

        results.append(FileRestoreInfo(
            path=path,
            content=content,
            timestamp=ts,
            token_count=token_count,
        ))
        used += token_count

    return results


def create_skill_attachments(
    max_tokens_per_skill: int = POST_COMPACT_MAX_TOKENS_PER_SKILL,
    token_budget: int = POST_COMPACT_SKILLS_TOKEN_BUDGET,
) -> List[SkillRestoreInfo]:
    """
    Return invoked skills that fit within the token budget.
    Mirrors CC's skill attachment logic in compactConversation().

    CC intentionally does NOT reset sentSkillNames on compact — re-injecting
    the full skill listing is pure cache creation cost. Only previously-invoked
    skills (those in invoked_skills) are re-attached.
    """
    sorted_skills = sorted(
        _state.invoked_skills.values(),
        key=lambda s: s.invoked_at,
        reverse=True,
    )

    results: List[SkillRestoreInfo] = []
    used = 0

    for skill in sorted_skills:
        content = skill.content
        token_count = skill.token_count

        if token_count > max_tokens_per_skill:
            content = content[: max_tokens_per_skill * 4]
            token_count = max_tokens_per_skill

        if used + token_count > token_budget:
            break

        results.append(SkillRestoreInfo(
            name=skill.name,
            path=skill.path,
            content=content,
            invoked_at=skill.invoked_at,
            token_count=token_count,
        ))
        used += token_count

    return results


def mark_post_compaction(context_id: Optional[str] = None) -> None:
    """Mark that compaction has completed (for logging/metrics)."""
    logger.debug(f"Compaction marked complete" + (f" ({context_id})" if context_id else ""))


__all__ = [
    "PostCompactState",
    "FileRestoreInfo",
    "SkillRestoreInfo",
    "get_post_compact_state",
    "reset_post_compact_state",
    "record_file_read",
    "record_skill_invocation",
    "run_post_compact_cleanup",
    "create_post_compact_file_attachments",
    "create_skill_attachments",
    "mark_post_compaction",
    "POST_COMPACT_MAX_FILES_TO_RESTORE",
    "POST_COMPACT_TOKEN_BUDGET",
    "POST_COMPACT_MAX_TOKENS_PER_FILE",
    "POST_COMPACT_MAX_TOKENS_PER_SKILL",
    "POST_COMPACT_SKILLS_TOKEN_BUDGET",
]
