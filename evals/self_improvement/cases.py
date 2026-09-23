"""Evaluation cases for the administrator system and the repository snapshots they run on.

A case plants a known defect in a copy of Grid (``sabotage``), gives the
administrator a user complaint (``request``) and says where the cause lives
(``locus``). The open ``dev`` scenarios are what the workshop sees; the hidden
``targets`` must flip from failing to passing and the hidden ``guards`` must not
regress. Negative cases (``kind: abstain``) plant nothing and expect no candidate.
"""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
CASES_DIR = HERE / "cases"
GUARDS_FILE = CASES_DIR / "_guards.yaml"
KINDS = {"fix", "new_system", "abstain"}

# Nothing an improvement may touch, whatever the request says: the controller,
# the operator's policies, the workshop plumbing and the evaluation itself.
FORBIDDEN = (
    "grid_control/*",
    "policies/*",
    "evals/*",
    "core/workshop.py",
    "tools/control_tools.py",
    "Dockerfile",
    "docker-compose.yml",
)
# Left out of the stable snapshot so the workshop cannot read the hidden suite.
EXCLUDED = ("evals",)
# The system under test comes from the working tree, like the Python code that
# runs it: its config, prompts and skills must match the tools being evaluated.
UNDER_TEST = ("examples/system-admin",)
# What Grid appends to every final answer; an answer check must not match it alone.
ANSWER_FOOTER = "An answer in any language.\n\nКонтекст ID: ctx-0a1b2c3d"


@dataclass(frozen=True)
class Edit:
    file: str
    find: str
    replace: str


@dataclass(frozen=True)
class Locus:
    file: str
    yaml_path: str | None = None

    @property
    def key(self) -> str:
        return f"{self.file}:{self.yaml_path}" if self.yaml_path else self.file

    def value(self, text: str | None) -> Any:
        """The content at this locus of the file's *text*: all of it, or one YAML value."""
        if text is None:
            return None
        if not self.yaml_path:
            return text
        try:
            node: Any = yaml.safe_load(text)
        except yaml.YAMLError:
            return "<invalid yaml>"
        for key in self.yaml_path.split("."):
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return node


@dataclass(frozen=True)
class Case:
    name: str
    kind: str
    request: str
    dev: tuple[dict, ...]
    targets: tuple[dict, ...]
    guards: tuple[dict, ...]
    sabotage: tuple[Edit, ...] = ()
    locus: tuple[Locus, ...] = ()
    allowed_paths: tuple[str, ...] = ()
    # Paths a new system must create, e.g. examples/invoices/config.yaml.
    must_create: tuple[str, ...] = ()
    notes: str = ""
    # Why the case is left out of suite runs (failed calibration), if it is.
    disabled: str = ""
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.name}: unknown kind {self.kind!r}")
        if self.kind == "fix" and not (self.sabotage and self.targets):
            raise ValueError(f"{self.name}: a fix case needs a sabotage and targets")
        if self.kind == "abstain" and self.targets:
            raise ValueError(f"{self.name}: an abstain case has no targets")
        names = [s["name"] for s in (*self.targets, *self.guards)]
        if len(set(names)) != len(names):
            raise ValueError(f"{self.name}: duplicate hidden scenario names")
        for scenario in self.targets:
            if not scenario["name"].startswith("t-"):
                raise ValueError(f"{self.name}: target names start with t-")
        for scenario in self.guards:
            if not scenario["name"].startswith("g-"):
                raise ValueError(f"{self.name}: guard names start with g-")
        for scenario in (*self.dev, *self.holdout):
            pattern = scenario.get("output_matches")
            if pattern and re.search(pattern, ANSWER_FOOTER):
                raise ValueError(
                    f"{self.name}: {scenario['name']} output_matches matches the footer Grid "
                    "appends to every answer, so it passes whatever the agent says"
                )

    @property
    def holdout(self) -> tuple[dict, ...]:
        return (*self.targets, *self.guards)

    @property
    def planted(self) -> tuple[str, ...]:
        """Text the sabotage introduced; a fix normally removes it."""
        return tuple(edit.replace for edit in self.sabotage if edit.replace.strip())

    def allowed(self, path: str) -> bool:
        return any(fnmatch(path, pattern) for pattern in self.allowed_paths)


def contains(text: str | None, fragment: str) -> bool:
    """Whether *fragment* occurs in *text*, whitespace runs matching any whitespace."""
    if text is None:
        return False
    return re.search(r"\s+".join(map(re.escape, fragment.split())), text) is not None


def forbidden(path: str) -> bool:
    return any(fnmatch(path, pattern) for pattern in FORBIDDEN)


def load_guards() -> dict[str, dict]:
    data = yaml.safe_load(GUARDS_FILE.read_text(encoding="utf-8"))
    return {scenario["name"]: scenario for scenario in data["guards"]}


def load_case(path: Path, guards: dict[str, dict] | None = None) -> Case:
    guards = load_guards() if guards is None else guards
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    own = [g for g in data.get("guards", []) if isinstance(g, dict)]
    shared = [guards[name] for name in data.get("guards", []) if isinstance(name, str)]
    return Case(
        name=data["name"],
        kind=data["kind"],
        request=data["request"].strip(),
        dev=tuple(data.get("dev", ())),
        targets=tuple(data.get("targets", ())),
        guards=tuple(shared + own),
        sabotage=tuple(Edit(**edit) for edit in data.get("sabotage", ())),
        locus=tuple(Locus(**locus) for locus in data.get("locus", ())),
        allowed_paths=tuple(data.get("allowed_paths", ())),
        must_create=tuple(data.get("must_create", ())),
        notes=data.get("notes", ""),
        disabled=data.get("disabled", ""),
    )


def load_cases(names: list[str] | None = None) -> list[Case]:
    guards = load_guards()
    cases = [
        load_case(path, guards)
        for path in sorted(CASES_DIR.glob("*.yaml"))
        if not path.name.startswith("_")
    ]
    if names:
        missing = set(names) - {case.name for case in cases}
        if missing:
            raise ValueError(f"Unknown cases: {', '.join(sorted(missing))}")
        # A case named explicitly runs even when disabled: that is how it is recalibrated.
        return [case for case in cases if case.name in names]
    return [case for case in cases if not case.disabled]


# -- snapshots -----------------------------------------------------------------

def git(path: Path, *args: str, data: bytes | None = None) -> str:
    result = subprocess.run(
        [
            "git", "-C", str(path),
            "-c", "user.name=Grid", "-c", "user.email=grid@local",
            "-c", "core.autocrlf=false",
            *args,
        ],
        input=data,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"git {' '.join(args[:2])} failed: {result.stderr.decode(errors='replace')[-1500:]}"
        )
    return result.stdout.decode("utf-8", errors="replace").strip()


def export_head(repo: Path, target: Path, ref: str = "HEAD") -> None:
    """Committed files of *ref*, without the evaluation suite: never the working tree."""
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", ref],
        capture_output=True,
        check=True,
    ).stdout
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(target, filter="data")
    for name in EXCLUDED:
        shutil.rmtree(target / name, ignore_errors=True)


def apply_edits(root: Path, edits: tuple[Edit, ...], *, reverse: bool = False) -> None:
    """Apply unique replacements; a case whose text drifted fails loudly.

    Whitespace runs match any whitespace, so a find text may be written on one
    line for a description folded over several (``>-``) in the file.
    """
    for edit in edits:
        path = root / edit.file
        text = path.read_text(encoding="utf-8")
        old, new = (edit.replace, edit.find) if reverse else (edit.find, edit.replace)
        pattern = re.compile(r"\s+".join(map(re.escape, old.split())))
        count = len(pattern.findall(text))
        if count != 1:
            raise ValueError(
                f"{edit.file}: expected the edited text exactly once, found {count}: {old[:80]!r}"
            )
        path.write_text(pattern.sub(lambda _: new, text), encoding="utf-8", newline="")


def overlay_working_tree(repo: Path, target: Path, paths: tuple[str, ...] = UNDER_TEST) -> None:
    """Replace *paths* of the snapshot with the operator's working-tree versions."""
    for relative in paths:
        source, destination = repo / relative, target / relative
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(source, destination, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def build_source(repo: Path, target: Path, case: Case, ref: str = "HEAD") -> dict[str, str]:
    """A one-commit repository holding the sabotaged Grid, plus controls on side branches.

    One root commit and a neutral message: the history must not reveal the
    defect. ``reference`` reverts the sabotage (the known good fix) and
    ``noop`` changes nothing that matters; both calibrate the metrics.
    """
    export_head(repo, target, ref)
    overlay_working_tree(repo, target)
    apply_edits(target, case.sabotage)
    git(target, "init", "--quiet", "--initial-branch", "stable")
    git(target, "add", "--all")
    git(target, "commit", "--quiet", "--no-verify", "-m", "Grid stable")
    stable = git(target, "rev-parse", "HEAD")
    refs = {"stable": stable}
    if case.sabotage:
        git(target, "checkout", "--quiet", "-b", "reference")
        apply_edits(target, case.sabotage, reverse=True)
        git(target, "commit", "--quiet", "--all", "--no-verify", "-m", "Reference fix")
        refs["reference"] = git(target, "rev-parse", "HEAD")
        git(target, "checkout", "--quiet", "stable")
    git(target, "checkout", "--quiet", "-b", "noop")
    readme = target / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n", encoding="utf-8", newline="")
    git(target, "commit", "--quiet", "--all", "--no-verify", "-m", "No-op change")
    refs["noop"] = git(target, "rev-parse", "HEAD")
    git(target, "checkout", "--quiet", "stable")
    return refs
