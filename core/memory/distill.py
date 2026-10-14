"""Heuristic RU+EN distillation of real conversations into memory records.

The distiller reads ordinary prose, not only ``decision: ...`` lines:

* user preferences and standing rules   -> L3 profile (scope ``user``) or L1 constraint;
* decisions, bugs/fixes, architecture and workflow facts, stated by either side
  (labelled lines, bullet sections under "Итог"/"Summary"/..., or sentences with a
  recognisable cue such as "we decided", "причина в", "исправил `x.py`");
* one L2 scenario per productive exchange (task -> outcome);
* optionally an L0 snapshot of each recent exchange.

Everything is deterministic, so running it again over the same text yields the same
records and the backend's content-hash deduplication keeps the store free of copies.
Tool/system messages are never read. Lines that look like secrets are skipped.
"""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .models import MemoryCategory, MemoryLayer, MemoryRecord, MemoryScope

C = MemoryCategory

MAX_ITEM = 400
MAX_LABELLED = 1000
MAX_PER_MESSAGE = 12
MAX_PER_SECTION = 8
MAX_EXCHANGES = 20

# label (lowercase) -> (category, is_summary)
_LABELS: dict[str, tuple[MemoryCategory, bool]] = {
    "decision": (C.DECISION, False), "decisions": (C.DECISION, False),
    "решение": (C.DECISION, False), "решения": (C.DECISION, False),
    "conclusion": (C.DECISION, True), "conclusions": (C.DECISION, True),
    "outcome": (C.DECISION, True), "result": (C.DECISION, True), "results": (C.DECISION, True),
    "summary": (C.DECISION, True), "tl;dr": (C.DECISION, True), "tldr": (C.DECISION, True),
    "done": (C.DECISION, True), "what was done": (C.DECISION, True),
    "changes": (C.DECISION, True), "changed": (C.DECISION, True),
    "итог": (C.DECISION, True), "итоги": (C.DECISION, True),
    "вывод": (C.DECISION, True), "выводы": (C.DECISION, True),
    "результат": (C.DECISION, True), "результаты": (C.DECISION, True),
    "что сделано": (C.DECISION, True), "сделано": (C.DECISION, True),
    "изменения": (C.DECISION, True), "что изменилось": (C.DECISION, True),
    "constraint": (C.CONSTRAINT, False), "constraints": (C.CONSTRAINT, False),
    "ограничение": (C.CONSTRAINT, False), "ограничения": (C.CONSTRAINT, False),
    "rule": (C.CONSTRAINT, False), "rules": (C.CONSTRAINT, False),
    "правило": (C.CONSTRAINT, False), "правила": (C.CONSTRAINT, False),
    "important": (C.CONSTRAINT, False), "warning": (C.CONSTRAINT, False),
    "requirement": (C.CONSTRAINT, False), "requirements": (C.CONSTRAINT, False),
    "важно": (C.CONSTRAINT, False), "внимание": (C.CONSTRAINT, False),
    "требование": (C.CONSTRAINT, False), "требования": (C.CONSTRAINT, False),
    "architecture": (C.ARCHITECTURE, False), "design": (C.ARCHITECTURE, False),
    "архитектура": (C.ARCHITECTURE, False), "дизайн": (C.ARCHITECTURE, False),
    "bug": (C.BUG, False), "bugs": (C.BUG, False), "issue": (C.BUG, False),
    "root cause": (C.BUG, False), "cause": (C.BUG, False),
    "fixed": (C.BUG, True), "fix": (C.BUG, False), "fixes": (C.BUG, True),
    "ошибка": (C.BUG, False), "ошибки": (C.BUG, False), "баг": (C.BUG, False),
    "проблема": (C.BUG, False), "причина": (C.BUG, False),
    "корневая причина": (C.BUG, False),
    "исправлено": (C.BUG, True), "исправление": (C.BUG, False), "исправления": (C.BUG, True),
    "preference": (C.PREFERENCE, False), "preferences": (C.PREFERENCE, False),
    "предпочтение": (C.PREFERENCE, False), "предпочтения": (C.PREFERENCE, False),
    "workflow": (C.WORKFLOW, False), "procedure": (C.WORKFLOW, False),
    "процесс": (C.WORKFLOW, False), "процедура": (C.WORKFLOW, False),
    "note": (C.NOTE, False), "fact": (C.NOTE, False), "facts": (C.NOTE, False),
    "заметка": (C.NOTE, False), "факт": (C.NOTE, False), "факты": (C.NOTE, False),
}
_LABEL_ALT = "|".join(re.escape(k) for k in sorted(_LABELS, key=len, reverse=True))
_BULLET = r"(?:[-*•–]\s+|\d{1,2}[.)]\s+)"
_INLINE = re.compile(
    rf"^\s*(?:{_BULLET})?(?:#{{1,6}}\s*)?(?:\*\*|__)?({_LABEL_ALT})(?:\*\*|__)?\s*[:：](?:\*\*|__)?\s*(.*)$",
    re.IGNORECASE)
_HEADER = re.compile(
    rf"^\s*(?:#{{1,6}}\s*(?:\*\*|__)?|(?:\*\*|__))\s*({_LABEL_ALT})\s*[:：]?\s*(?:\*\*|__)?\s*$",
    re.IGNORECASE)
_BULLET_LINE = re.compile(rf"^\s*{_BULLET}(.*)$")
_HEADING = re.compile(r"^\s*#{1,6}\s")

_ARTEFACT = re.compile(
    r"`[^`\n]{2,80}`|(?<![\w/])[\w./-]+\.(?:py|js|mjs|cjs|ts|tsx|jsx|yaml|yml|json|toml|md|html|css|sql|sh|cfg|ini)\b")
_SECRET = re.compile(
    r"(?:api[_ -]?key|token|passw(?:or)?d|secret|пароль|токен|ключ api)\s*(?:is|—|[:=])\s*\S+|\bsk-[\w-]{12,}|"
    r"\bghp_\w{12,}|\bBearer\s+\w{12,}", re.IGNORECASE)

_X = re.IGNORECASE
_PREFERENCE = re.compile("|".join((
    r"\bi (?:would |'d )?prefer\b", r"\bprefer\w*\b", r"\bfrom now on\b", r"\bgoing forward\b",
    r"\bin (?:the )?future\b", r"\bremember (?:that|to)\b", r"\bkeep in mind\b", r"\bby default\b",
    r"\b(?:answer|reply|respond|write|speak|communicate|explain|comment)\w*\b[^.!?\n]{0,30}"
    r"\b(?:in|using) (?:russian|english|german|ukrainian|short|brief|detail\w*|simple|plain|concise)\b",
    r"\bkeep (?:the )?(?:answers?|responses?|replies|explanations?|comments?) (?:short|brief|concise|detailed)\b",
    r"\bпредпочита\w*", r"\bпредпочт\w*", r"\bвпредь\b", r"\bв дальнейшем\b", r"\bна будущее\b",
    r"\bпо умолчанию\b", r"\bзапомни\b", r"\bне забывай\b", r"\bмне (?:нравится|удобн\w+|важно)\b",
    r"\bя (?:люблю|хочу,? чтобы)\b",
    r"\b(?:отвечай|пиши|общайся|говори|объясняй|комментируй)\w*\b[^.!?\n]{0,40}"
    r"\b(?:на (?:русском|английском|украинском)|по-русски|по-английски|кратко|коротко|подробно|простым языком)\b",
)), _X)
_ALWAYS_NEVER = re.compile("|".join((
    r"\b(?:always|never)\b", r"\b(?:don't|do not|dont) (?:ever |use |add |write |touch |change |create |modify |commit )",
    r"\b(?:всегда|никогда)\b",
    r"\bне (?:надо|нужно|используй\w*|делай\w*|пиши\w*|добавляй\w*|трогай\w*|меняй\w*|создавай\w*|коммить\w*)\b",
)), _X)
_CONSTRAINT = re.compile("|".join((
    r"\bmust(?: not|n't)?\b", r"\bshould not\b", r"\bshouldn't\b", r"\bis required\b", r"\brequired to\b",
    r"\bforbidden\b", r"\bnot allowed\b", r"\bhas to\b", r"\bhave to\b",
    r"\bнельзя\b", r"\bзапрещено\b", r"\bобязательно\b", r"\bдолжен\b", r"\bдолжна\b", r"\bдолжны\b",
    r"\bнеобходимо\b", r"\bтребуется\b", r"\bне допускается\b",
)), _X)
_BUG = re.compile("|".join((
    r"\broot cause\b", r"\bcaused by\b", r"\bthe (?:bug|issue|problem) (?:is|was)\b", r"\bbug in\b",
    r"\b(?:fixed|resolved|patched)\b", r"\bregression\b", r"\brace condition\b", r"\bmemory leak\b",
    r"\bкорневая причина\b", r"\bпричина\b[^.!?\n]{0,25}(?:в|—|:)", r"\bисправил\w*", r"\bисправлен\w*",
    r"\bпочинил\w*", r"\bустранил\w*", r"\bустранен\w*", r"\bбаг\w*", r"\bутечк\w*", r"\bгонк\w* (?:данных|состояний)\b",
    r"\bошибк\w* (?:в|была|возникает)\b", r"\bрегресс\w*",
)), _X)
_DECISION = re.compile("|".join((
    r"\bwe(?:'ve| have)? decided\b", r"\bdecided to\b", r"\bwe(?:'ll| will) (?:use|go with|keep|switch|move|store)\b",
    r"\bgoing with\b", r"\b(?:i|we) chose\b", r"\blet'?s (?:use|go with|keep|switch|store)\b",
    r"\bagreed (?:to|on)\b", r"\bthe decision is\b", r"\bswitch(?:ed|ing) to\b", r"\bmigrat\w+ to\b",
    r"\breplac\w+ [^.!?\n]{1,40} with\b", r"\bimplemented\b", r"\badded\b",
    r"\bреши(?:ли|л|ла)\b", r"\bпринято решение\b", r"\bдоговорил\w+", r"\bвыбра(?:ли|л|ла)\b",
    r"\bбудем (?:использовать|делать|применять|хранить)\b",
    r"\bдавай(?:те)? (?:использовать|сделаем|оставим|перейд\w+|хранить)\b", r"\bпереход\w* на\b",
    r"\bзамени(?:ли|л|ла)\b", r"\bоставим\b", r"\bостановились на\b",
    r"\bреализова\w+", r"\bдобавил\w*", r"\bсоздал\w*",
)), _X)
_ARCHITECTURE = re.compile("|".join((
    r"\barchitectur\w*", r"\bdesign pattern\b", r"\blayered\b", r"\bseparation of concerns\b", r"\bdata flow\b",
    r"\bis responsible for\b", r"\blives in\b", r"\bimplemented in\b", r"\bstored in\b", r"\bhandled by\b",
    r"\bentry point\b",
    r"\bархитектур\w*", r"\bслой\b", r"\bслои\b", r"\bпаттерн\w*", r"\bотвечает за\b", r"\bнаходится в\b",
    r"\bреализован\w* в\b", r"\bхранится в\b", r"\bобрабатывается в\b", r"\bточка входа\b", r"\bпоток данных\b",
)), _X)
_WORKFLOW = re.compile("|".join((
    r"\brun (?:the )?tests?\b", r"\bbefore (?:commit|merge|release|push|deploy)\w*\b", r"\bworkflow\b",
    r"\bprocedure\b", r"\bchecklist\b", r"\bstep[- ]by[- ]step\b",
    r"\bзапуска\w+ тест\w*", r"\bперед (?:коммитом|мержем|релизом|пушем|деплоем)\b", r"\bпроцедур\w*",
    r"\bчек-?лист\b", r"\bпорядок действий\b",
)), _X)
_USER_DECISION = re.compile("|".join((
    r"\bwe(?:'ve| have)? decided\b", r"\bdecided to\b", r"\bwe(?:'ll| will) (?:use|go with|keep|switch|store)\b",
    r"\blet'?s (?:use|go with|keep|switch|store)\b", r"\bagreed (?:to|on)\b", r"\bswitch(?:ed|ing) to\b",
    r"\bреши(?:ли|л|ла)\b", r"\bпринято решение\b", r"\bдоговорил\w+", r"\bбудем (?:использовать|делать|применять|хранить)\b",
    r"\bдавай(?:те)? (?:использовать|сделаем|оставим|перейд\w+|хранить)\b", r"\bпереход\w* на\b", r"\bоставим\b",
)), _X)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+(?=[A-ZА-ЯЁ0-9`\"'(\[])")
_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_INLINE_MARKUP = re.compile(r"(\*\*|__)")
_SUMMARY_LABELS = frozenset(k for k, (_, is_summary) in _LABELS.items() if is_summary)


@dataclass(frozen=True, slots=True)
class _Item:
    category: MemoryCategory
    layer: MemoryLayer
    scope: MemoryScope
    text: str
    confidence: float
    summary: bool = False


def text_of(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        return "\n".join(
            part.get("text", "") for part in content
            if isinstance(part, Mapping) and part.get("type") in ("text", "input_text", "output_text")
            and isinstance(part.get("text", ""), str))
    return ""


def _clean(text: str, limit: int = MAX_ITEM) -> str:
    text = _INLINE_MARKUP.sub("", text).strip().strip("*_ \t")
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _usable(text: str, minimum: int = 12) -> bool:
    if len(text) < minimum or text.endswith("?") or _SECRET.search(text):
        return False
    letters = sum(ch.isalpha() for ch in text)
    return letters >= len(text) * 0.4 and text[0] not in "|{[<$>#"


def _sentences(line: str) -> list[str]:
    return [part for part in _SENTENCE_SPLIT.split(line.strip()) if part.strip()]


def _classify(sentence: str, role: str) -> _Item | None:
    words = len(sentence.split())
    if words < 3:
        return None
    artefact = bool(_ARTEFACT.search(sentence))
    if role == "user":
        if _PREFERENCE.search(sentence):
            return _Item(C.PREFERENCE, MemoryLayer.L3, MemoryScope.USER, sentence, 0.7)
        if _ALWAYS_NEVER.search(sentence):
            if artefact:
                return _Item(C.CONSTRAINT, MemoryLayer.L1, MemoryScope.PROJECT, sentence, 0.6)
            return _Item(C.PREFERENCE, MemoryLayer.L3, MemoryScope.USER, sentence, 0.6)
        if _CONSTRAINT.search(sentence):
            return _Item(C.CONSTRAINT, MemoryLayer.L1, MemoryScope.PROJECT, sentence, 0.6)
        if _USER_DECISION.search(sentence):
            return _Item(C.DECISION, MemoryLayer.L1, MemoryScope.PROJECT, sentence, 0.6)
        return None
    # Assistant prose: a cue alone is too weak; it needs a concrete artefact or enough substance.
    substantial = artefact or words >= 7
    if not substantial:
        return None
    for pattern, category, needs_artefact in (
        (_BUG, C.BUG, False), (_DECISION, C.DECISION, True),
        (_ARCHITECTURE, C.ARCHITECTURE, True), (_WORKFLOW, C.WORKFLOW, False),
    ):
        if pattern.search(sentence) and (artefact or not needs_artefact):
            return _Item(category, MemoryLayer.L1, MemoryScope.PROJECT, sentence, 0.55)
    return None


def _item(category: MemoryCategory, text: str, confidence: float, summary: bool = False) -> _Item:
    if category == C.PREFERENCE:
        return _Item(category, MemoryLayer.L3, MemoryScope.USER, text, confidence, summary)
    return _Item(category, MemoryLayer.L1, MemoryScope.PROJECT, text, confidence, summary)


def scan_text(text: str, role: str) -> list[_Item]:
    """Distil one message into candidate items (labelled lines, sections, sentences)."""
    items: list[_Item] = []
    section: tuple[MemoryCategory, bool] | None = None
    section_count = 0
    for raw in _FENCE.sub(" ", text).splitlines():
        if len(items) >= MAX_PER_MESSAGE:
            break
        line = raw.rstrip()
        if not line.strip():
            if section is not None and section_count:
                section = None
            continue
        header = _HEADER.match(line)
        if header:
            section, section_count = _LABELS[header[1].casefold()], 0
            continue
        inline = _INLINE.match(line)
        if inline:
            category, is_summary = _LABELS[inline[1].casefold()]
            rest = _clean(inline[2], MAX_LABELLED)
            if rest:
                if _usable(rest, 3):
                    items.append(_item(category, rest, 0.7, is_summary))
                section = None
            else:
                section, section_count = (category, is_summary), 0
            continue
        if _HEADING.match(line):
            section = None
            continue
        bullet = _BULLET_LINE.match(line)
        body = _clean(bullet[1] if bullet else line, MAX_LABELLED if section else MAX_ITEM)
        if section is not None and section_count < MAX_PER_SECTION and (bullet or section_count == 0):
            if _usable(body, 6):
                category, is_summary = section
                items.append(_item(category, body, 0.65, is_summary))
                section_count += 1
            continue
        if section is not None and not bullet and section_count:
            section = None
        for sentence in _sentences(_clean(line, 2000)):
            sentence = _clean(sentence)
            if not _usable(sentence, 15):
                continue
            found = _classify(sentence, role)
            if found:
                items.append(found)
                if len(items) >= MAX_PER_MESSAGE:
                    break
    return items


def _tags(text: str) -> tuple[str, ...]:
    tags: list[str] = []
    for match in _ARTEFACT.finditer(text):
        tag = match[0].strip("`")
        if "/" in tag or "." in tag:
            tag = tag.rsplit("/", 1)[-1]
        if 2 < len(tag) <= 60 and " " not in tag and tag not in tags:
            tags.append(tag)
        if len(tags) >= 5:
            break
    return tuple(tags)


def _exchanges(messages: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """(user request, assistant reply) pairs; replies between two requests are joined."""
    pairs: list[list[str]] = []
    for message in messages:
        role, text = message.get("role"), text_of(message).strip()
        if role not in ("user", "assistant") or not text:
            continue
        if role == "user":
            pairs.append([text, ""])
        elif pairs:
            pairs[-1][1] = (pairs[-1][1] + "\n\n" + text).strip()
    return [(user, assistant) for user, assistant in pairs if assistant]


def _outcome(assistant: str, items: list[_Item]) -> str:
    summary = [item.text for item in items if item.summary][:4]
    if summary:
        return "; ".join(summary)
    sentences: list[str] = []
    for line in _FENCE.sub(" ", assistant).splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "|")):
            continue
        for sentence in _sentences(_clean(_BULLET_LINE.sub(r"\1", line), 2000)):
            sentence = _clean(sentence)
            if _usable(sentence, 20):
                sentences.append(sentence)
            if len(sentences) >= 2:
                return " ".join(sentences)
    return " ".join(sentences)


def distill_messages(messages: Sequence[Mapping[str, Any]], *, snapshot: bool = False) -> list[MemoryRecord]:
    """Records for a conversation: L1/L3 facts and rules, L2 scenarios and, optionally, L0."""
    records: list[MemoryRecord] = []
    seen: set[tuple[str, str]] = set()

    def add(item: _Item) -> None:
        marker = (item.category.value + item.layer.value, re.sub(r"\W+", " ", item.text.casefold()).strip())
        if marker in seen:
            return
        seen.add(marker)
        records.append(MemoryRecord(
            content=item.text, category=item.category, layer=item.layer, scope=item.scope,
            confidence=item.confidence, tags=_tags(item.text)))

    per_message: dict[int, list[_Item]] = {}
    for index, message in enumerate(messages):
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = text_of(message)
        if text:
            per_message[index] = scan_text(text, role)
            for item in per_message[index]:
                add(item)

    for user, assistant in _exchanges(messages)[-MAX_EXCHANGES:]:
        user_clean, assistant_clean = _clean(user, 300), _clean(_FENCE.sub(" ", assistant), 100000)
        if snapshot:
            records.append(MemoryRecord(
                content=f"User: {user[:1500]}\n\nAssistant: {assistant[:3000]}",
                layer=MemoryLayer.L0, category=C.NOTE, scope=MemoryScope.PROJECT,
                confidence=1.0, tags=("chat-snapshot",)))
        if len(user_clean) >= 15 and len(assistant_clean) >= 120 and not _SECRET.search(user_clean):
            outcome = _outcome(assistant, scan_text(assistant, "assistant"))
            if outcome and not _SECRET.search(outcome):
                records.append(MemoryRecord(
                    content=f"Task: {user_clean}\nOutcome: {_clean(outcome, 500)}",
                    layer=MemoryLayer.L2, category=C.NOTE, scope=MemoryScope.PROJECT,
                    confidence=0.5, tags=_tags(user + " " + outcome)))
    return records
