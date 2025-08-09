from __future__ import annotations

import json
import difflib
from pathlib import Path
from typing import Dict, Any, Optional

from .unified_logger import (
    add_log_listener,
    LogEventType,
    LogLevel,
    get_unified_logger,
)

# Простое состояние по агенту для накопления рассуждений
_reasoning_buffer: dict[str, list[str]] = {}


def _safe_get(d: Optional[Dict[str, Any]], key: str, default=None):
    if not isinstance(d, dict):
        return default
    return d.get(key, default)


def _parse_args_from_tool_event_data(event_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Извлечь аргументы инструмента из event.data.args, поддерживая строковый JSON."""
    args = _safe_get(event_data, "args")
    if args is None:
        return {}
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            return json.loads(args)
        except Exception:
            return {}
    return {}


def _on_event_reasoning(event, logger):
    """Слушатель: извлекает шаги/мысли из sequentialthinking и логирует как REASONING."""
    agent = event.agent_name or "unknown"
    # Инициализация буфера на старте
    if event.event_type == LogEventType.AGENT_START:
        _reasoning_buffer[agent] = []
        return

    # Фиксируем финальный свод на завершение
    if event.event_type == LogEventType.AGENT_END:
        steps = _reasoning_buffer.get(agent) or []
        if steps:
            text = "\n".join(steps)
            logger.log(
                LogEventType.REASONING,
                message="Сводный план и рассуждения",
                agent_name=agent,
                data={"text": text, "__enriched__": True},
                level=LogLevel.DEBUG,
            )
        return

    # Вызов sequentialthinking: достанем текст мысли из аргументов
    if event.event_type == LogEventType.TOOL_CALL and (event.tool_name or "") == "sequentialthinking":
        arg_obj = _parse_args_from_tool_event_data(event.data)
        # часто аргументы вложены под ключом "args" строкой JSON
        nested = arg_obj.get("args")
        try:
            if isinstance(nested, str):
                nested = json.loads(nested)
        except Exception:
            nested = None
        thought_text = None
        if isinstance(nested, dict):
            t = nested.get("thought")
            tn = nested.get("thoughtNumber")
            tt = nested.get("totalThoughts")
            if isinstance(t, str) and t.strip():
                if tn is not None and tt is not None:
                    thought_text = f"Шаг {tn}/{tt}: " + t.strip()
                else:
                    thought_text = t.strip()
        if thought_text:
            # Сократим слишком длинное
            preview = thought_text if len(thought_text) <= 1200 else (thought_text[:1200] + "…")
            _reasoning_buffer.setdefault(agent, []).append(preview)
            logger.log(
                LogEventType.REASONING,
                message="Промежуточный план",
                agent_name=agent,
                data={"text": preview, "__enriched__": True},
                level=LogLevel.DEBUG,
            )

    # Интересен результат sequentialthinking
    if event.event_type == LogEventType.TOOL_RESULT and (event.tool_name or "") == "sequentialthinking":
        result = _safe_get(event.data, "result", "")
        # result приходит строкой; пробуем JSON
        thought_str = None
        try:
            parsed = json.loads(result)
            # популярные поля: thoughtNumber, totalThoughts, nextThoughtNeeded
            tn = parsed.get("thoughtNumber")
            tt = parsed.get("totalThoughts")
            next_needed = parsed.get("nextThoughtNeeded")
            # иногда ветки/доп. инфо
            branches = parsed.get("branches")
            line = []
            if tn is not None and tt is not None:
                line.append(f"Шаг {tn}/{tt}")
            if next_needed is not None:
                line.append("нужен следующий шаг" if next_needed else "последний шаг")
            if branches:
                line.append(f"веток: {len(branches)}")
            if not line:
                # если нет ожидаемых полей — сохраним компактный JSON
                thought_str = json.dumps(parsed, ensure_ascii=False)
            else:
                thought_str = " | ".join(line)
        except Exception:
            # если не JSON — сократим
            if isinstance(result, str) and result.strip():
                text = result.strip()
                thought_str = text if len(text) <= 400 else (text[:400] + "…")
        if not thought_str:
            return
        _reasoning_buffer.setdefault(agent, []).append(thought_str)
        # Промежуточный вывод шага
        logger.log(
            LogEventType.REASONING,
            message="Промежуточный шаг планирования",
            agent_name=agent,
            data={"text": thought_str, "__enriched__": True},
            level=LogLevel.DEBUG,
        )


def _on_event_diff(event, logger):
    """Слушатель: строит diff для write_file до записи, чтобы видеть изменения."""
    if event.event_type != LogEventType.TOOL_CALL:
        return
    if (event.tool_name or "") != "write_file":
        return

    args = _parse_args_from_tool_event_data(event.data)
    path = args.get("path") or args.get("file_path")
    content = args.get("content")
    if not path or content is None:
        return

    try:
        p = Path(path)
        before_text = ""
        if p.exists():
            before_text = p.read_text(encoding="utf-8", errors="ignore")
        after_text = str(content)
        # Ограничим слишком большие тексты
        max_chars = 20000
        if len(before_text) > max_chars:
            before_text = before_text[:max_chars]
        if len(after_text) > max_chars:
            after_text = after_text[:max_chars]
        diff_lines = list(
            difflib.unified_diff(
                before_text.splitlines(),
                after_text.splitlines(),
                fromfile=f"a/{p.name}",
                tofile=f"b/{p.name}",
                lineterm="",
                n=3,
            )
        )
        if not diff_lines:
            return
        # Возьмём первые N строк для читаемости
        head = 200
        diff_preview = "\n".join(diff_lines[:head])
        logger.log(
            LogEventType.DIFF,
            message=f"Предстоящая запись файла: {p}",
            agent_name=event.agent_name,
            data={"diff": diff_preview, "__enriched__": True},
            level=LogLevel.INFO,
        )
    except Exception:
        # Никогда не мешаем основному выполнению
        return


def register_default_enrichers() -> None:
    """Идемпотентная регистрация стандартных обогащателей логов."""
    logger = get_unified_logger()
    add_log_listener("reasoning_enricher", _on_event_reasoning)
    add_log_listener("diff_enricher", _on_event_diff) 