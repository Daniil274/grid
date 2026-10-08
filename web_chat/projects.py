"""Projects: a small hierarchy of metadata over a space's conversations.

A project is nothing but a named node - ``workspace -> project -> subproject``,
at most :data:`MAX_DEPTH` levels deep - and the chats of the space point at one
through their ``project_id`` metadata (see :mod:`web_chat.projects_api` and
``UserSpace.conversations_by_project``). Nothing here touches the conversations
file: this store owns exactly one file, ``projects.json``, written whole or not
at all (``tmp`` + :func:`os.replace`, as in :mod:`web_chat.review.store`), and
guarded by its own :class:`threading.Lock` - never the space's, so a project
edit cannot serialise behind a running turn.

Old chats have no ``project_id`` and need no migration: they simply show as
"no project".
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

#: The document version, so a future change can migrate in place.
VERSION = 1

#: The longest a project title may be, after whitespace is collapsed.
MAX_TITLE = 120
#: The longest a project description may be (markdown).
MAX_DESCRIPTION = 4000
#: The longest the project's shared notes may be (markdown).
MAX_NOTES = 32768
#: How deeply projects nest: a root project is depth 1.
MAX_DEPTH = 3


class ProjectError(ValueError):
    """A project cannot be stored as asked for (bad parent, cycle, too deep)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_title(title: Any) -> str:
    clean = " ".join(str(title).split())
    if not clean:
        raise ProjectError("A project needs a title")
    return clean[:MAX_TITLE]


def _clip(text: Any, limit: int) -> str:
    if text is None:
        return ""
    return str(text)[:limit]


@dataclass
class Project:
    """One node of the project hierarchy."""

    id: str
    title: str
    parent_id: Optional[str] = None
    description: str = ""
    notes: str = ""
    archived: bool = False
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "parent_id": self.parent_id,
            "description": self.description,
            "notes": self.notes,
            "archived": self.archived,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Project":
        return cls(
            id=str(data["id"]),
            title=str(data.get("title", "")),
            parent_id=data.get("parent_id") or None,
            description=data.get("description") or "",
            notes=data.get("notes") or "",
            archived=bool(data.get("archived", False)),
            created_at=data.get("created_at") or _now(),
            updated_at=data.get("updated_at") or _now(),
        )


class ProjectStore:
    """The projects of one space, in one ``projects.json``."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._projects: Dict[str, Project] = {}
        self._load()

    # -- reading ------------------------------------------------------------------
    def get(self, project_id: str) -> Optional[Project]:
        """The project, or None."""
        with self._lock:
            project = self._projects.get(project_id)
            return _copy(project) if project is not None else None

    def list(self) -> List[Project]:
        """Every project, in creation order (the order they were written)."""
        with self._lock:
            return [_copy(project) for project in self._projects.values()]

    def exists(self, project_id: str) -> bool:
        with self._lock:
            return project_id in self._projects

    # -- writing ------------------------------------------------------------------
    def create(self, title: str, parent_id: Optional[str] = None, description: str = "") -> Project:
        """Add a project; raise :class:`ProjectError` for a bad parent or too deep."""
        with self._lock:
            parent_id = parent_id or None
            self._validate_parent(parent_id, moving_id=None)
            now = _now()
            project = Project(
                id=f"prj-{uuid.uuid4().hex[:8]}",
                title=_clean_title(title),
                parent_id=parent_id,
                description=_clip(description, MAX_DESCRIPTION),
                created_at=now,
                updated_at=now,
            )
            self._projects[project.id] = project
            self._write()
            return _copy(project)

    def update(self, project_id: str, **fields: Any) -> Project:
        """Change a project's fields; ``KeyError`` if unknown, :class:`ProjectError` on a bad move.

        Only the keys given are touched, so ``parent_id=None`` moves it to the
        root while an absent ``parent_id`` leaves it where it is.
        """
        with self._lock:
            project = self._projects.get(project_id)
            if project is None:
                raise KeyError(project_id)
            if "parent_id" in fields:
                parent_id = fields["parent_id"] or None
                if parent_id != project.parent_id:
                    self._validate_parent(parent_id, moving_id=project_id)
                project.parent_id = parent_id
            if "title" in fields and fields["title"] is not None:
                project.title = _clean_title(fields["title"])
            if "description" in fields and fields["description"] is not None:
                project.description = _clip(fields["description"], MAX_DESCRIPTION)
            if "notes" in fields and fields["notes"] is not None:
                project.notes = _clip(fields["notes"], MAX_NOTES)
            if "archived" in fields and fields["archived"] is not None:
                project.archived = bool(fields["archived"])
            project.updated_at = _now()
            self._write()
            return _copy(project)

    def delete(self, project_id: str) -> Tuple[Optional[str], List[str]]:
        """Remove a project; its direct children are re-parented to its parent.

        Returns ``(parent_id, child_ids)`` - the parent its children (and, by
        the caller, its chats) moved to, and the ids of those children. A chat
        re-parenting is the API's, which alone owns the conversations.
        """
        with self._lock:
            project = self._projects.pop(project_id, None)
            if project is None:
                raise KeyError(project_id)
            parent_id = project.parent_id
            moved: List[str] = []
            for other in self._projects.values():
                if other.parent_id == project_id:
                    other.parent_id = parent_id
                    other.updated_at = _now()
                    moved.append(other.id)
            self._write()
            return parent_id, moved

    # -- invariants ------------------------------------------------------------------
    def _validate_parent(self, parent_id: Optional[str], moving_id: Optional[str]) -> None:
        """The parent must exist, not descend from the moved project, and never
        make the tree deeper than :data:`MAX_DEPTH`."""
        if parent_id is None:
            return
        if parent_id not in self._projects:
            raise ProjectError("The parent project does not exist")
        if moving_id is not None:
            ancestor: Optional[Project] = self._projects[parent_id]
            while ancestor is not None:
                if ancestor.id == moving_id:
                    raise ProjectError("A project cannot be nested inside itself")
                ancestor = self._projects.get(ancestor.parent_id) if ancestor.parent_id else None
        base = self._depth(parent_id) + 1
        below = self._subtree_height(moving_id) if moving_id is not None else 1
        if base + below - 1 > MAX_DEPTH:
            raise ProjectError(f"Projects nest at most {MAX_DEPTH} levels deep")

    def _depth(self, project_id: str) -> int:
        """The project's depth: a root is 1."""
        depth = 1
        node = self._projects.get(project_id)
        seen: set[str] = set()
        while node is not None and node.parent_id and node.id not in seen:
            seen.add(node.id)
            node = self._projects.get(node.parent_id)
            if node is None:
                break
            depth += 1
        return depth

    def _subtree_height(self, project_id: Optional[str]) -> int:
        """How many levels the project and its descendants span; 1 for a leaf."""
        if project_id is None:
            return 1
        children = [other for other in self._projects.values() if other.parent_id == project_id]
        if not children:
            return 1
        return 1 + max(self._subtree_height(child.id) for child in children)

    # -- the file ------------------------------------------------------------------
    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._projects = {}
            return
        except (json.JSONDecodeError, OSError) as exc:
            raise ProjectError(f"The projects file {self.path} cannot be read: {exc}") from exc
        projects: Dict[str, Project] = {}
        for item in raw.get("projects", []) if isinstance(raw, dict) else []:
            project = Project.from_dict(item)
            projects[project.id] = project
        self._projects = projects

    def _write(self) -> None:
        document = {"version": VERSION, "projects": [project.to_dict() for project in self._projects.values()]}
        temporary = self.path.with_name(self.path.name + ".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)


def _copy(project: Project) -> Project:
    """A detached copy, so a caller cannot mutate the store's state."""
    return Project(**project.to_dict())
