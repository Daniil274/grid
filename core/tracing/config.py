"""
Tracing configuration for the Grid system.
Replaces loggers with Agents SDK tracing without calling OpenAI.
"""

import os
from typing import List, Optional, Any
from agents.tracing import set_trace_processors
from agents.tracing.processors import BatchTraceProcessor
from agents.tracing.processor_interface import TracingExporter, TracingProcessor
from agents.tracing.traces import Trace
from agents.tracing.spans import Span
import json
import logging


class ConsoleSpanExporter(TracingExporter):
    """Trace exporter to console with pretty formatting."""
    
    def __init__(self, level: str = "INFO"):
        self.level = level.upper()
        # Compact mode by default (INFO): minimum lines and syntax
        self._compact = self.level in ("INFO", "COMPACT", "MINIMAL", "LOW")
        # Suppress repeated MCP list tools for the same server
        self._printed_mcp_servers: set[str] = set()
    
    def export(self, items: list[Trace | Span[Any]]) -> None:
        """Export trace to console with pretty formatting."""
        for item in items:
            try:
                data = item.export()
                if not data:
                    continue
                
                # Format output based on type
                if data.get("object") == "trace":
                    self._print_trace(data)
                elif data.get("object") == "trace.span":
                    self._print_span(data)
                else:
                    # Don't print raw objects to console
                    return
            except Exception as e:
                print(f"❌ Trace export error: {e}")
    
    def _print_trace(self, data: dict):
        """Pretty print a trace."""
        # trace_id = data.get("id", "unknown")
        # workflow = data.get("workflow_name", "Unknown")
        # print(f"\n🚀 TRACE START: {workflow}")
        # print(f"   ID: {trace_id}")
        # if not self._compact and data.get("metadata"):
        #     print(f"   Metadata: {self._format_kv_table(data['metadata'])}")
    
    def _print_span(self, data: dict):
        """Pretty print a span."""
        span_data = data.get("span_data", {})
        span_type = span_data.get("type", "unknown")
        
        # Icons
        icons = {
            "agent": "🤖",
            "generation": "💭", 
            "function": "🔧",
            "mcp_tools": "🧩",
        }
        icon = icons.get(span_type, "•")
        
        name = span_data.get("name") or span_data.get("server") or ""
        duration = self._calculate_duration(data)
        dur = f" ⏱ {duration}" if duration else ""
        
        # Details by type (compact)
        if span_type == "agent":
            # Hide agent tracing in compact mode
            if self._compact:
                return
            print(f"{icon} {name or 'agent'}{dur}")
            return
        
        if span_type == "generation":
            # Hide generation details in compact mode
            if self._compact:
                return
            usage = span_data.get("usage") or {}
            model = span_data.get("model")
            it = usage.get("input_tokens")
            ot = usage.get("output_tokens")
            parts = []
            if model:
                parts.append(f"model={model}")
            if it is not None and ot is not None:
                parts.append(f"tokens={it}→{ot}")
            line = f"{icon} gen"
            if parts:
                line += " " + ", ".join(parts)
            line += dur
            print(line)
            self._print_generation_io(span_data)
            return
        
        if span_type == "function":
            # Tool calls are rendered by the stream observer with agent context.
            # Avoid duplicate compact trace lines such as "tool 123ms".
            if self._compact:
                return
            # Single-line, no JSON
            mcp_data = span_data.get("mcp_data") or {}
            mcp_hint = ""
            server = mcp_data.get("server")
            tool = mcp_data.get("tool") or name
            if server:
                mcp_hint = f" · MCP {server}.{tool}"
            print(f"{icon} {name or 'function'}{mcp_hint}{dur}")
            if not self._compact:
                self._print_function_io(span_data)
            return
        
        if span_type == "mcp_tools":
            # In compact mode, don't print at all
            if self._compact:
                return
            # Print once per server, no enumerations
            server = span_data.get("server") or "unknown"
            if server in self._printed_mcp_servers:
                return
            tools = span_data.get("result") or []
            print(f"{icon} MCP {server}: {len(tools)} tool(s)")
            self._printed_mcp_servers.add(server)
            return
        
        if span_type == "handoff":
            # Human-readable handoff: AgentA → AgentB
            src = span_data.get("from_agent") or "agent"
            dst = span_data.get("to_agent") or "agent"
            print(f"🔀 {src} → {dst}{dur}")
            return
        
        # Other types — single line
        if name:
            print(f"{icon} {span_type} {name}{dur}")
        else:
            print(f"{icon} {span_type}{dur}")
    
    def _print_generation_io(self, span_data: dict) -> None:
        input_seq = span_data.get("input")
        output_seq = span_data.get("output")
        if input_seq:
            print("   in:")
            for msg in self._humanize_messages(input_seq)[:3]:
                print(f"     {msg}")
            if len(input_seq) > 3:
                print(f"     … +{len(input_seq) - 3}")
        if output_seq:
            print("   out:")
            for msg in self._humanize_messages(output_seq)[:2]:
                print(f"     {msg}")
            if len(output_seq) > 2:
                print(f"     … +{len(output_seq) - 2}")
    
    def _print_function_info(self, span_data: dict):
        # Not used — kept for compatibility
        self._print_function_io(span_data)
    
    def _print_function_io(self, span_data: dict) -> None:
        input_data = span_data.get("input")
        output_data = span_data.get("output")
        if input_data:
            print(f"   in: {self._humanize_value(input_data, max_len=160)}")
        if output_data is not None:
            print(f"   out: {self._humanize_value(output_data, max_len=200)}")
    
    def _calculate_duration(self, data: dict) -> str:
        """Calculate span duration."""
        try:
            from datetime import datetime
            started = data.get("started_at")
            ended = data.get("ended_at")
            
            if started and ended:
                start_time = datetime.fromisoformat(started.replace('Z', '+00:00'))
                end_time = datetime.fromisoformat(ended.replace('Z', '+00:00'))
                duration = end_time - start_time
                
                total_seconds = duration.total_seconds()
                if total_seconds < 1:
                    return f"{total_seconds * 1000:.0f}ms"
                else:
                    return f"{total_seconds:.2f}s"
        except Exception:
            pass
        return ""
    
    # ===== Helpers =====
    def _trim(self, value: Any, max_len: int) -> str:
        s = str(value)
        return s if len(s) <= max_len else s[: max_len - 1] + "…"
    
    def _humanize_value(self, value: Any, max_len: int = 200) -> str:
        """Short human-readable representation without JSON brackets and quotes around keys."""
        try:
            if isinstance(value, (dict, list)):
                if isinstance(value, dict):
                    items = []
                    for k, v in list(value.items())[:10]:
                        items.append(f"{k}={self._one_line(v, 100)}")
                    s = ", ".join(items)
                else:  # list
                    s = ", ".join(self._one_line(v, 80) for v in value[:10])
            else:
                s = str(value)
        except Exception:
            s = str(value)
        if len(s) > max_len:
            s = s[: max_len - 1] + "…"
        return s
    
    def _one_line(self, v: Any, max_len: int) -> str:
        s = str(v)
        s = s.replace("\n", " ⏎ ")
        if len(s) > max_len:
            s = s[: max_len - 1] + "…"
        if s.startswith("'") and s.endswith("'") and len(s) <= max_len:
            s = s[1:-1]
        return s
    
    def _humanize_messages(self, seq: Any) -> list[str]:
        result: list[str] = []
        try:
            for msg in seq:
                role = msg.get("role") if isinstance(msg, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, list):
                    parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            parts.append(part.get("text", ""))
                    content_str = " ".join(parts) if parts else str(content)
                else:
                    content_str = str(content)
                role_str = f"{role}: " if role else ""
                result.append(role_str + self._one_line(content_str, 160))
        except Exception:
            result = [self._one_line(seq, 160)]
        return result
    
    def _format_kv_table(self, d: dict) -> str:
        try:
            items = [f"{k}={self._one_line(v, 60)}" for k, v in list(d.items())[:10]]
            return ", ".join(items)
        except Exception:
            return str(d)


class FileSpanExporter(TracingExporter):
    """Exports spans to a JSONL file for analysis."""
    
    def __init__(self, path: str = "traces/traces.json"):
        self.path = path
        self.logger = logging.getLogger("grid.tracing")
        
    def export(self, items: List[Trace | Span]) -> None:
        """Export spans to file."""
        try:
            # ensure directory exists
            import os as _os
            _dir = _os.path.dirname(self.path)
            if _dir and not os.path.exists(_dir):
                os.makedirs(_dir, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                for item in items:
                    data = item.export()
                    if data:
                        f.write(json.dumps(data, ensure_ascii=False) + "\n")
            self.logger.debug(f"Exported {len(items)} traces to {self.path}")
        except Exception as e:
            self.logger.error(f"Failed to export traces to {self.path}: {e}")


class HttpSpanExporter(TracingExporter):
    """Exports spans via HTTP to a local or self-hosted service."""
    
    def __init__(self, endpoint: str, timeout: float = 0.1):
        self.endpoint = endpoint
        self.timeout = timeout
        self.logger = logging.getLogger("grid.tracing")
        
        # Import httpx only when needed
        try:
            import httpx
            self.client = httpx.Client(timeout=timeout)
            self._httpx_available = True
        except ImportError:
            self.logger.warning("httpx not available, HTTP export disabled")
            self._httpx_available = False
        
    def export(self, items: List[Trace | Span]) -> None:
        """Export spans via HTTP."""
        if not self._httpx_available:
            return
            
        try:
            data = [i.export() for i in items if i.export()]
            if data:
                response = self.client.post(
                    self.endpoint, 
                    json={"data": data},
                    headers={"Content-Type": "application/json"}
                )
                response.raise_for_status()
                self.logger.debug(f"Exported {len(data)} traces to {self.endpoint}")
        except Exception as e:
            self.logger.error(f"Failed to export traces to {self.endpoint}: {e}")


class ImmediateTraceProcessor(TracingProcessor):
    """Synchronous processor that immediately exports traces/spans without a background queue."""
    def __init__(self, exporter: TracingExporter, export_span_start: bool = False):
        self._exporter = exporter
        self._export_span_start = export_span_start

    def on_trace_start(self, trace: Trace) -> None:
        try:
            self._exporter.export([trace])
        except Exception:
            pass

    def on_trace_end(self, trace: Trace) -> None:
        # Do nothing — already exported on start
        pass

    def on_span_start(self, span: Span[Any]) -> None:
        if self._export_span_start:
            try:
                self._exporter.export([span])
            except Exception:
                pass

    def on_span_end(self, span: Span[Any]) -> None:
        try:
            self._exporter.export([span])
        except Exception:
            pass

    def shutdown(self, timeout: float | None = None):
        pass

    def force_flush(self):
        pass


class TracingConfig:
    """Tracing configuration for the Grid system."""
    
    def __init__(self):
        self._configured = False
        self._processors = []
        
    def configure_console_tracing(self, level: str = "INFO") -> None:
        """Configure console tracing."""
        if self._configured:
            return
            
        exporter = ConsoleSpanExporter(level)
        # Immediate export to console
        processor = ImmediateTraceProcessor(exporter)
        self._processors = [processor]
        self._configured = True
        
    def configure_file_tracing(self, path: str = "traces.jsonl") -> None:
        """Configure file tracing."""
        if self._configured:
            return
            
        exporter = FileSpanExporter(path)
        # For files, also use immediate export
        processor = ImmediateTraceProcessor(exporter)
        self._processors = [processor]
        self._configured = True
        
    def configure_http_tracing(self, endpoint: str, timeout: float = 0.1) -> None:
        """Configure HTTP tracing."""
        if self._configured:
            return
            
        exporter = HttpSpanExporter(endpoint, timeout)
        # Immediate export so events aren't delayed
        processor = ImmediateTraceProcessor(exporter)
        self._processors = [processor]
        self._configured = True
        
    def configure_custom_tracing(self, exporters: List[TracingExporter]) -> None:
        """Configure custom tracing with multiple exporters."""
        if self._configured:
            return
            
        processors = [ImmediateTraceProcessor(exporter) for exporter in exporters]
        self._processors = processors
        self._configured = True
        
    def apply(self) -> None:
        """Apply tracing configuration."""
        if not self._configured:
            # Default to console tracing
            self.configure_console_tracing()
            
        # Set tracing processors
        set_trace_processors(self._processors)
        
    def disable(self) -> None:
        """Disable tracing."""
        from agents.tracing import set_trace_processors
        set_trace_processors([])
        self._configured = False


# Global tracing configuration instance
tracing_config = TracingConfig()


def configure_tracing_from_env() -> None:
    """Configure tracing based on environment variables."""
    tracing_type = os.getenv("GRID_TRACING_TYPE", "console").lower()
    tracing_level = os.getenv("GRID_TRACING_LEVEL", "INFO")
    file_path = os.getenv("GRID_TRACING_FILE", "traces/traces.jsonl")

    if tracing_type == "file":
        tracing_config.configure_file_tracing(file_path)
    elif tracing_type == "http":
        endpoint = os.getenv("GRID_TRACING_ENDPOINT", "http://localhost:8080/ingest")
        timeout = float(os.getenv("GRID_TRACING_TIMEOUT", "0.1"))
        tracing_config.configure_http_tracing(endpoint, timeout)
    elif tracing_type in ("console", "both", "multi"):
        # Default: both console and file — visible in CLI immediately and saved to disk
        exporters: List[TracingExporter] = [
            ConsoleSpanExporter(tracing_level),
            FileSpanExporter(file_path),
        ]
        tracing_config.configure_custom_tracing(exporters)
    elif tracing_type == "disabled":
        tracing_config.disable()
        return
    else:
        # Default: console + file
        exporters = [ConsoleSpanExporter(tracing_level), FileSpanExporter(file_path)]
        tracing_config.configure_custom_tracing(exporters)

    # Timeline tracer: always enabled (unless explicitly disabled)
    if os.getenv("GRID_TIMELINE_ENABLED", "true").lower() not in ("0", "false", "no"):
        try:
            from core.tracing.tracer import get_tracer
            timeline_exporter = get_tracer()
            # Use export_span_start=True to see running status in real time
            timeline_processor = ImmediateTraceProcessor(timeline_exporter, export_span_start=True)
            tracing_config._processors.append(timeline_processor)
            # #region agent log
            try:
                import time
                _log = {"id": "log_timeline_registered", "timestamp": time.time() * 1000, "location": "core/tracing_config.py:configure_tracing_from_env", "message": "timeline tracer registered", "data": {"db_path": str(getattr(timeline_exporter, "_db_path", "?"))}, "runId": "agent", "hypothesisId": "H1"}
                with open("/home/user/grid/.cursor/debug-11be9a.log", "a") as f:
                    f.write(json.dumps(_log, ensure_ascii=False) + "\n")
            except Exception:
                pass
            # #endregion
        except Exception as e:
            logging.getLogger("grid.tracing").warning(f"Timeline tracer init failed: {e}")

    # Apply configuration
    tracing_config.apply()


def get_tracing_config() -> TracingConfig:
    """Returns the global tracing configuration."""
    return tracing_config 
