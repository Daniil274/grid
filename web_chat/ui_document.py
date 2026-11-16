"""A small, declarative UI document an answer may carry.

The model composes *data* - known components, values, bindings - and the server
checks all of it; the client renders only that catalog. Nothing here is
executable: no HTML, no JavaScript, no CSS, no URL, no event-handler string, no
arbitrary expression. A node the catalog does not know is refused instead of
being guessed at, and an unknown field on a known node is refused too, so a
document cannot smuggle a property the client would later start honouring.

The catalog of this first slice is deliberately narrow: ``stack``, ``text``,
``metric``, ``table``, ``select`` and ``button``. That is enough for the one
scenario the design note starts with - compare a few options, switch the
criterion locally, pick one, discuss it - and small enough to check honestly.

Limits are part of the contract, not a suggestion: a document that is too big,
too deep, too wide or too long is refused with a code the caller can act on.
The wire format keeps ``fallback_markdown`` beside the tree, so a client that
cannot render the block (an old build, plain text, an export) still shows the
answer.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Optional

#: The only document version this build understands. A newer one is refused,
#: never interpreted half-way.
SCHEMA_VERSION = 1

#: What a document may cost, in bytes of UTF-8 JSON of its whole body.
MAX_DOCUMENT_BYTES = 131_072
MAX_NODES = 300
MAX_DEPTH = 8
MAX_ROWS = 100
MAX_COLUMNS = 12
MAX_TEXT = 4_000
MAX_FALLBACK = 8_000
MAX_PROMPT = 1_000
MAX_ID = 40
MAX_STRING = 500

#: Tuples of allowed field names per component. A field outside its tuple is
#: an error: the client never sees data the server did not check by name.
_NODE_FIELDS: dict[str, tuple[str, ...]] = {
    "stack": ("id", "type", "title", "gap", "children"),
    "text": ("id", "type", "text"),
    "metric": ("id", "type", "label", "value", "unit", "hint"),
    "table": ("id", "type", "title", "columns", "rows", "row_label", "sort", "sort_state", "select_state"),
    "select": ("id", "type", "label", "bind", "options"),
    "button": ("id", "type", "label", "action", "tone"),
}
TOP_FIELDS = (
    "schema_version", "id", "revision", "title", "fallback_markdown",
    "state_schema", "initial_state", "root", "actions",
)
#: Where a bound state value may come from; each is checked against the node
#: that owns it (a select's options, a table's columns and rows).
STATE_TYPES = ("enum", "sort", "row")
ACTION_KINDS = ("submit_to_agent",)
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
_SORT_VALUE = re.compile(r"^(?P<column>[A-Za-z][A-Za-z0-9_-]{0,39}):(?P<direction>asc|desc)$")


class UiDocumentError(ValueError):
    """A document the build refuses, with a machine code for the caller."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _fail(code: str, message: str) -> None:
    raise UiDocumentError(code, message)


def _string(value: Any, where: str, *, limit: int = MAX_STRING, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        _fail("ui.schema", f"{where} must be a string")
    if not allow_empty and not value.strip():
        _fail("ui.schema", f"{where} must not be empty")
    if len(value) > limit:
        _fail("ui.limit", f"{where} is longer than {limit} characters")
    return value


def _identifier(value: Any, where: str) -> str:
    if not isinstance(value, str) or not _ID.match(value):
        _fail("ui.schema", f"{where} must be an id (a letter, then letters, digits, '_' or '-', at most {MAX_ID})")
    return value


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("ui.schema", f"{where} must be an object")
    return value


def _only_fields(value: dict[str, Any], allowed: Iterable[str], where: str) -> None:
    allowed = tuple(allowed)
    for key in value:
        if key not in allowed:
            _fail("ui.schema", f"{where} has an unknown field {key!r}")


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _scalar(value: Any, where: str) -> Any:
    """A table cell: text, a number or a boolean. Never a nested object."""
    if isinstance(value, bool) or _number(value):
        return value
    if isinstance(value, str):
        return _string(value, where, limit=MAX_STRING, allow_empty=True)
    _fail("ui.schema", f"{where} must be text, a number or a boolean")


# -- state -----------------------------------------------------------------

def _state_schema(raw: Any) -> dict[str, dict[str, Any]]:
    schema = _object(raw, "state_schema")
    if len(schema) > 12:
        _fail("ui.limit", "state_schema has more than 12 keys")
    checked: dict[str, dict[str, Any]] = {}
    for key, spec in schema.items():
        _identifier(key, "a state key")
        spec = _object(spec, f"state_schema.{key}")
        kind = spec.get("type")
        if kind not in STATE_TYPES:
            _fail("ui.schema", f"state_schema.{key}.type must be one of {', '.join(STATE_TYPES)}")
        if kind == "sort":
            _only_fields(spec, ("type", "table", "label"), f"state_schema.{key}")
            entry = {"type": "sort", "table": _identifier(spec.get("table"), f"state_schema.{key}.table")}
        elif kind == "row":
            _only_fields(spec, ("type", "table", "label"), f"state_schema.{key}")
            entry = {"type": "row", "table": _identifier(spec.get("table"), f"state_schema.{key}.table")}
        else:
            _only_fields(spec, ("type", "values", "label"), f"state_schema.{key}")
            values = spec.get("values")
            if not isinstance(values, list) or not values or len(values) > 50:
                _fail("ui.schema", f"state_schema.{key}.values must be a non-empty list of at most 50")
            entry = {"type": "enum", "values": [_string(v, f"state_schema.{key}.values", limit=MAX_STRING) for v in values]}
        if "label" in spec:
            entry["label"] = _string(spec["label"], f"state_schema.{key}.label", limit=120)
        checked[key] = entry
    return checked


def _values(document: dict[str, Any], raw: Any, where: str) -> dict[str, Any]:
    """Checked state values: every key of the schema, nothing else."""
    values = _object(raw, where)
    schema = document["state_schema"]
    for key in values:
        if key not in schema:
            _fail("ui.bad_state", f"{where} has a value for unknown state key {key!r}")
    checked: dict[str, Any] = {}
    for key, spec in schema.items():
        if key not in values:
            _fail("ui.bad_state", f"{where} is missing state key {key!r}")
        checked[key] = _state_value(document, key, spec, values[key], where)
    return checked


def _state_value(document: dict[str, Any], key: str, spec: dict[str, Any], value: Any, where: str) -> Any:
    kind = spec["type"]
    if kind == "enum":
        if value not in spec["values"]:
            _fail("ui.bad_state", f"{where}.{key} is not one of the allowed values")
        return value
    if kind == "sort":
        if not isinstance(value, str):
            _fail("ui.bad_state", f"{where}.{key} must be a sort value like column:asc")
        found = _SORT_VALUE.match(value)
        if not found:
            _fail("ui.bad_state", f"{where}.{key} must be a sort value like column:asc")
        table = _table(document, spec["table"])
        columns = {column["key"] for column in table["columns"]}
        if found["column"] not in columns:
            _fail("ui.bad_state", f"{where}.{key} names a column the table does not have")
        return value
    # A row of *table*; checked against the rows the document actually carries.
    if not isinstance(value, str):
        _fail("ui.bad_state", f"{where}.{key} must be a row id")
    table = _table(document, spec["table"])
    if value not in {row["id"] for row in table["rows"]}:
        _fail("ui.bad_state", f"{where}.{key} names a row the table does not have")
    return value


def _table(document: dict[str, Any], table_id: str) -> dict[str, Any]:
    found = _find(document["root"], table_id)
    if found is None or found.get("type") != "table":
        _fail("ui.schema", f"state_schema names {table_id!r}, which is not a table of this document")
    return found


def _find(node: dict[str, Any], node_id: str) -> Optional[dict[str, Any]]:
    if node.get("id") == node_id:
        return node
    for child in node.get("children") or ():
        found = _find(child, node_id)
        if found is not None:
            return found
    return None


# -- nodes -----------------------------------------------------------------

def _node(raw: Any, depth: int, seen: set[str], budget: list[int]) -> dict[str, Any]:
    node = _object(raw, "a node")
    if depth > MAX_DEPTH:
        _fail("ui.limit", f"the tree is deeper than {MAX_DEPTH}")
    budget[0] -= 1
    if budget[0] < 0:
        _fail("ui.limit", f"the tree has more than {MAX_NODES} nodes")
    node_type = node.get("type")
    if not isinstance(node_type, str) or node_type not in _NODE_FIELDS:
        _fail("ui.unknown_component", f"unknown component {node_type!r}")
    _only_fields(node, _NODE_FIELDS[node_type], f"node {node_type}")
    node_id = _identifier(node.get("id"), f"{node_type}.id")
    if node_id in seen:
        _fail("ui.schema", f"duplicate node id {node_id!r}")
    seen.add(node_id)
    checked: dict[str, Any] = {"type": node_type, "id": node_id}
    if node_type == "stack":
        gap = node.get("gap", "m")
        if gap not in ("s", "m", "l"):
            _fail("ui.schema", f"stack {node_id} has an unknown gap {gap!r}")
        checked["gap"] = gap
        if "title" in node:
            checked["title"] = _string(node["title"], f"stack {node_id}.title", limit=120)
        children = node.get("children")
        if not isinstance(children, list) or not children:
            _fail("ui.schema", f"stack {node_id} needs a non-empty children list")
        checked["children"] = [_node(child, depth + 1, seen, budget) for child in children]
    elif node_type == "text":
        checked["text"] = _string(node.get("text"), f"text {node_id}.text", limit=MAX_TEXT)
    elif node_type == "metric":
        checked["label"] = _string(node.get("label"), f"metric {node_id}.label", limit=120)
        value = node.get("value")
        checked["value"] = value if isinstance(value, str) or _number(value) else _fail(
            "ui.schema", f"metric {node_id}.value must be text or a number"
        )
        if "unit" in node:
            checked["unit"] = _string(node["unit"], f"metric {node_id}.unit", limit=24)
        if "hint" in node:
            checked["hint"] = _string(node["hint"], f"metric {node_id}.hint", limit=200)
    elif node_type == "table":
        if "title" in node:
            checked["title"] = _string(node["title"], f"table {node_id}.title", limit=120)
        columns = node.get("columns")
        if not isinstance(columns, list) or not columns or len(columns) > MAX_COLUMNS:
            _fail("ui.schema", f"table {node_id} needs 1..{MAX_COLUMNS} columns")
        checked_columns = []
        keys: set[str] = set()
        for column in columns:
            column = _object(column, f"table {node_id}.columns")
            _only_fields(column, ("key", "label", "align"), f"table {node_id}.columns")
            key = _identifier(column.get("key"), f"table {node_id} column key")
            if key in keys:
                _fail("ui.schema", f"table {node_id} repeats column {key!r}")
            keys.add(key)
            entry = {"key": key, "label": _string(column.get("label"), f"table {node_id} column label", limit=80)}
            align = column.get("align", "start")
            if align not in ("start", "end"):
                _fail("ui.schema", f"table {node_id} column {key} has an unknown align {align!r}")
            entry["align"] = align
            checked_columns.append(entry)
        checked["columns"] = checked_columns
        rows = node.get("rows")
        if not isinstance(rows, list) or not rows:
            _fail("ui.schema", f"table {node_id} needs a non-empty rows list")
        if len(rows) > MAX_ROWS:
            _fail("ui.limit", f"table {node_id} has more than {MAX_ROWS} rows")
        checked_rows = []
        row_ids: set[str] = set()
        for row in rows:
            row = _object(row, f"table {node_id} row")
            _only_fields(row, tuple(keys) + ("id",), f"table {node_id} row")
            row_id = row.get("id")
            if row_id is None:
                _fail("ui.schema", f"table {node_id} row needs an id")
            row_id = _identifier(row_id, f"table {node_id} row id")
            if row_id in row_ids:
                _fail("ui.schema", f"table {node_id} repeats row {row_id!r}")
            row_ids.add(row_id)
            values = {"id": row_id}
            for key in keys:
                if key not in row:
                    _fail("ui.schema", f"table {node_id} row {row_id} is missing column {key!r}")
                values[key] = _scalar(row[key], f"table {node_id} row {row_id}.{key}")
            checked_rows.append(values)
        checked["rows"] = checked_rows
        if "row_label" in node:
            label_key = _identifier(node["row_label"], f"table {node_id}.row_label")
            if label_key not in keys:
                _fail("ui.schema", f"table {node_id}.row_label is not a column")
            checked["row_label"] = label_key
        if "sort" in node:
            checked["sort"] = _sort_spec(node["sort"], keys, f"table {node_id}.sort")
        if "sort_state" in node:
            checked["sort_state"] = _identifier(node["sort_state"], f"table {node_id}.sort_state")
        if "select_state" in node:
            checked["select_state"] = _identifier(node["select_state"], f"table {node_id}.select_state")
    elif node_type == "select":
        checked["label"] = _string(node.get("label"), f"select {node_id}.label", limit=120)
        checked["bind"] = _identifier(node.get("bind"), f"select {node_id}.bind")
        options = node.get("options")
        if not isinstance(options, list) or not options or len(options) > 50:
            _fail("ui.schema", f"select {node_id} needs 1..50 options")
        checked_options = []
        values = set()
        for option in options:
            option = _object(option, f"select {node_id} option")
            _only_fields(option, ("value", "label"), f"select {node_id} option")
            value = _string(option.get("value"), f"select {node_id} option value", limit=MAX_STRING)
            if value in values:
                _fail("ui.schema", f"select {node_id} repeats option {value!r}")
            values.add(value)
            checked_options.append({"value": value, "label": _string(option.get("label"), f"select {node_id} option label", limit=80)})
        checked["options"] = checked_options
    else:  # button
        checked["label"] = _string(node.get("label"), f"button {node_id}.label", limit=80)
        checked["action"] = _identifier(node.get("action"), f"button {node_id}.action")
        tone = node.get("tone", "default")
        if tone not in ("default", "primary"):
            _fail("ui.schema", f"button {node_id} has an unknown tone {tone!r}")
        checked["tone"] = tone
    return checked


def _sort_spec(raw: Any, keys: set[str], where: str) -> dict[str, str]:
    spec = _object(raw, where)
    _only_fields(spec, ("column", "direction"), where)
    column = spec.get("column")
    if column not in keys:
        _fail("ui.schema", f"{where}.column is not a column of this table")
    direction = spec.get("direction", "asc")
    if direction not in ("asc", "desc"):
        _fail("ui.schema", f"{where}.direction must be asc or desc")
    return {"column": column, "direction": direction}


def _actions(raw: Any) -> dict[str, dict[str, Any]]:
    actions = _object(raw, "actions")
    if len(actions) > 8:
        _fail("ui.limit", "actions has more than 8 entries")
    checked: dict[str, dict[str, Any]] = {}
    for key, spec in actions.items():
        _identifier(key, "an action id")
        spec = _object(spec, f"actions.{key}")
        _only_fields(spec, ("kind", "label", "prompt"), f"actions.{key}")
        kind = spec.get("kind")
        if kind not in ACTION_KINDS:
            _fail("ui.unknown_action", f"actions.{key}.kind must be one of {', '.join(ACTION_KINDS)}")
        entry = {"kind": kind, "label": _string(spec.get("label"), f"actions.{key}.label", limit=80)}
        if "prompt" in spec:
            entry["prompt"] = _string(spec["prompt"], f"actions.{key}.prompt", limit=MAX_PROMPT, allow_empty=True)
        checked[key] = entry
    return checked


def _check_bindings(document: dict[str, Any]) -> None:
    """Every state binding names a key of the right kind, and is used once.

    A sort or row key is legitimately held by two controls: the table that
    displays the order (or the chosen row) and the select that lets the person
    change it. What is refused is a *second* control of the same kind - two
    selects writing one value, two tables claiming one order - and a key no
    control can change at all.
    """
    schema = document["state_schema"]
    selects: set[str] = set()
    tables: set[str] = set()

    def visit(node: dict[str, Any]) -> None:
        if node["type"] == "select":
            key = node["bind"]
            spec = schema.get(key)
            if spec is None:
                _fail("ui.schema", f"select {node['id']} binds unknown state key {key!r}")
            # A select may offer an enum, or a table's sort orders or rows: its
            # own options are the choices, and each is held to the state's rule.
            offered = [option["value"] for option in node["options"]]
            if spec["type"] == "enum":
                if set(offered) != set(spec["values"]):
                    _fail("ui.schema", f"select {node['id']} does not offer exactly the values of {key!r}")
            else:
                for value in offered:
                    _state_value(document, key, spec, value, f"select {node['id']}")
            if key in selects:
                _fail("ui.schema", f"state key {key!r} is bound by more than one select")
            if key in tables and spec["type"] == "enum":
                _fail("ui.schema", f"state key {key!r} is bound more than once")
            selects.add(key)
        for field, kind in (("sort_state", "sort"), ("select_state", "row")):
            key = node.get(field)
            if key is None:
                continue
            spec = schema.get(key)
            if spec is None:
                _fail("ui.schema", f"table {node['id']} binds unknown state key {key!r}")
            if spec["type"] != kind or spec["table"] != node["id"]:
                _fail("ui.schema", f"table {node['id']} binds {key!r} to another kind of value")
            if key in tables:
                _fail("ui.schema", f"state key {key!r} is bound by more than one table")
            tables.add(key)
        for child in node.get("children") or ():
            visit(child)

    visit(document["root"])
    for key in schema:
        if key not in selects and key not in tables:
            _fail("ui.schema", f"state key {key!r} is not bound to any control")


def validate_document(raw: Any) -> dict[str, Any]:
    """Check *raw* and return its canonical form; raise :class:`UiDocumentError`.

    The canonical form is what gets stored, shown and later read back: the
    check is the same one for a model, a test and the server's own storage.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            _fail("ui.schema", f"the document is not JSON: {exc}")
    document = _object(raw, "the document")
    _only_fields(document, TOP_FIELDS, "the document")
    size = len(json.dumps(document, ensure_ascii=False).encode("utf-8"))
    if size > MAX_DOCUMENT_BYTES:
        _fail("ui.limit", f"the document is larger than {MAX_DOCUMENT_BYTES} bytes")
    if document.get("schema_version") != SCHEMA_VERSION:
        _fail("ui.schema", f"schema_version must be {SCHEMA_VERSION}")
    checked: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": _identifier(document.get("id"), "the document id"),
    }
    revision = document.get("revision", 1)
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        _fail("ui.schema", "revision must be a positive integer")
    checked["revision"] = revision
    if "title" in document:
        checked["title"] = _string(document["title"], "title", limit=120)
    checked["fallback_markdown"] = _string(
        document.get("fallback_markdown"), "fallback_markdown", limit=MAX_FALLBACK
    )
    checked["state_schema"] = _state_schema(document.get("state_schema", {}))
    checked["root"] = _node(document.get("root"), 1, set(), [MAX_NODES])
    checked["actions"] = _actions(document.get("actions", {}))
    _check_bindings(checked)
    _check_buttons(checked)
    checked["initial_state"] = _values(checked, document.get("initial_state", {}), "initial_state")
    _check_initial_choices(checked)
    return checked


def _check_initial_choices(document: dict[str, Any]) -> None:
    """A select must be able to show the value its bound key starts with.

    Without this a document could start on a sort (or row) the select does not
    offer: the page would show the first option while the table used another
    value, and the first interaction would silently overwrite the real choice.
    """
    initial = document["initial_state"]

    def visit(node: dict[str, Any]) -> None:
        if node["type"] == "select":
            key = node["bind"]
            offered = {option["value"] for option in node["options"]}
            if initial[key] not in offered:
                _fail(
                    "ui.schema",
                    f"select {node['id']} does not offer the initial value {initial[key]!r} of {key!r}",
                )
        for child in node.get("children") or ():
            visit(child)

    visit(document["root"])


def _check_buttons(document: dict[str, Any]) -> None:
    def visit(node: dict[str, Any]) -> None:
        if node["type"] == "button" and node["action"] not in document["actions"]:
            _fail("ui.unknown_action", f"button {node['id']} names unknown action {node['action']!r}")
        for child in node.get("children") or ():
            visit(child)

    visit(document["root"])


def validate_values(document: dict[str, Any], raw: Any, *, where: str = "values") -> dict[str, Any]:
    """Check a state payload against an already validated document."""
    return _values(document, raw, where)


def action_of(document: dict[str, Any], action_id: Any) -> dict[str, Any]:
    """The action *action_id* of *document*, or a refusal naming it."""
    if not isinstance(action_id, str) or action_id not in document["actions"]:
        _fail("ui.unknown_action", f"the document has no action {action_id!r}")
    return document["actions"][action_id]


def value_labels(document: dict[str, Any], values: dict[str, Any]) -> list[str]:
    """One human line per state value: ``Label: chosen value``.

    A select's option label and a row's label column are read from the document,
    so the sentence the agent receives says what the person saw, not an id.
    """
    schema = document["state_schema"]
    lines: list[str] = []
    for key, spec in schema.items():
        value = values.get(key)
        label = spec.get("label") or key
        shown = value
        if spec["type"] == "enum":
            select = _select_for(document["root"], key)
            if select is not None:
                labels = {option["value"]: option["label"] for option in select["options"]}
                shown = labels.get(value, value)
        elif spec["type"] == "row":
            table = _table(document, spec["table"])
            row = next((row for row in table["rows"] if row["id"] == value), None)
            if row is not None:
                shown = row.get(table.get("row_label") or "", value)
        elif spec["type"] == "sort":
            table = _table(document, spec["table"])
            found = _SORT_VALUE.match(str(value))
            if found:
                column = next((c for c in table["columns"] if c["key"] == found["column"]), None)
                arrow = "ascending" if found["direction"] == "asc" else "descending"
                shown = f"{column['label'] if column else found['column']}, {arrow}"
        lines.append(f"- {label}: {shown}")
    return lines


def _select_for(node: dict[str, Any], key: str) -> Optional[dict[str, Any]]:
    if node["type"] == "select" and node["bind"] == key:
        return node
    for child in node.get("children") or ():
        found = _select_for(child, key)
        if found is not None:
            return found
    return None
