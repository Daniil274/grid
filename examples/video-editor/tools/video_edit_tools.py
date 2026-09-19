"""Video edit and render tools for the video-editor example."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from agents import function_tool
from agents.tool import ToolOutputText

from _video_common import (
    VideoToolError,
    clean_filename,
    display_path,
    duration_seconds,
    ensure_parent,
    format_srt_time,
    format_time,
    make_video_output,
    parse_time,
    probe_media,
    read_json_arg,
    require_success,
    resolve_path,
    run_process,
    which,
    write_json,
)


def _ok_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _error(exc: Exception) -> str:
    return f"ERROR: {exc}"


def _coerce_segments(payload: Any) -> tuple[str | None, list[dict[str, Any]], dict[str, Any]]:
    """Return (input_path, raw_segments, metadata) from several accepted JSON shapes."""
    if isinstance(payload, list):
        return None, payload, {}
    if not isinstance(payload, dict):
        raise VideoToolError("Cutlist input must be a JSON object or array.")

    input_path = payload.get("input") or payload.get("source") or payload.get("video")
    metadata = {k: v for k, v in payload.items() if k not in {"segments", "shots", "recommended_cutlist"}}

    if isinstance(payload.get("segments"), list):
        return input_path, payload["segments"], metadata

    recommended = payload.get("recommended_cutlist") or {}
    if isinstance(recommended, dict) and isinstance(recommended.get("segments"), list):
        return input_path, recommended["segments"], metadata

    shots = payload.get("shots")
    if isinstance(shots, list):
        included = [shot for shot in shots if shot.get("include") is True]
        if included:
            return input_path, included, metadata
        return input_path, shots, metadata

    raise VideoToolError("No segments found. Provide segments[], recommended_cutlist.segments[], or shots[].")


def _segment_source(item: dict[str, Any], default_source: str | None) -> str:
    """Per-segment source path, falling back to the cutlist-wide one."""
    source = item.get("source") or item.get("input") or item.get("video") or item.get("file")
    return str(source or default_source or "").strip()


def _source_duration(path: Path) -> float:
    """Duration of *path* in seconds, probed once per render."""
    key = str(path)
    if key not in _DURATION_CACHE:
        _DURATION_CACHE[key] = duration_seconds(probe_media(path))
    return _DURATION_CACHE[key]


_DURATION_CACHE: dict[str, float] = {}


def _normalize_segment(item: dict[str, Any], index: int, default_source: str | None = None) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise VideoToolError(f"Segment #{index} must be an object.")

    source = _segment_source(item, default_source)
    if not source:
        raise VideoToolError(f"Segment #{index} has no source video (set source, or pass input_path).")
    source_path = resolve_path(source, must_exist=True)

    start_raw = item.get("start", item.get("in", item.get("from")))
    end_raw = item.get("end", item.get("out", item.get("to")))
    duration_raw = item.get("duration")
    start = parse_time(start_raw) if start_raw is not None else 0.0
    if end_raw is not None:
        end = parse_time(end_raw)
    elif duration_raw is not None:
        end = start + float(duration_raw)
    else:
        # No end given: take the segment to the end of its own source file.
        end = _source_duration(source_path)

    if start < 0:
        raise VideoToolError(f"Segment #{index} starts before zero.")
    if end <= start:
        raise VideoToolError(f"Segment #{index} end must be after start.")

    label = str(item.get("label") or item.get("id") or f"segment_{index:03d}")
    return {
        "id": clean_filename(str(item.get("id") or f"segment_{index:03d}"), f"segment_{index:03d}"),
        "label": label,
        "source": display_path(source_path),
        "start": round(start, 3),
        "end": round(end, 3),
        "start_timecode": format_time(start),
        "end_timecode": format_time(end),
        "duration": round(end - start, 3),
        "notes": str(item.get("notes") or item.get("description") or ""),
    }


@function_tool
def video_build_cutlist(
    segments_json: str,
    output_path: str = "edits/cutlist.json",
    input_path: str = "",
    title: str = "video edit",
) -> str:
    """
    Normalize annotation/segment JSON into an edit decision list.

    Args:
        segments_json: JSON string or workspace path. Accepts segments[], shots[], or recommended_cutlist.segments[].
            Each segment may carry its own "source" path, so one cutlist can join several files.
            A segment without start/end covers its whole source file.
        output_path: Destination JSON path.
        input_path: Default source video for segments that do not name their own.
        title: Human-readable edit title.

    Returns:
        JSON summary with the cutlist path and total selected duration.
    """
    try:
        payload = read_json_arg(segments_json, default_name="segments_json")
        embedded_input, raw_segments, metadata = _coerce_segments(payload)
        default_source = input_path.strip() or embedded_input
        out_path = resolve_path(output_path)
        segments = [
            _normalize_segment(item, idx, default_source) for idx, item in enumerate(raw_segments, start=1)
        ]
        total = round(sum(seg["duration"] for seg in segments), 3)
        sources = list(dict.fromkeys(seg["source"] for seg in segments))
        cutlist = {
            "schema": "grid.video.cutlist.v1",
            "title": title,
            "input": sources[0],
            "inputs": sources,
            "created_at_unix": int(time.time()),
            "segment_count": len(segments),
            "total_duration": total,
            "segments": segments,
            "source_metadata": metadata,
        }
        write_json(out_path, cutlist)
        return _ok_json(
            {
                "output": display_path(out_path),
                "inputs": sources,
                "segment_count": len(segments),
                "total_duration": total,
            }
        )
    except Exception as exc:
        return _error(exc)


@function_tool
def video_make_preview(
    input_path: str,
    output_path: str = "renders/preview.mp4",
    start: str = "0",
    duration: float = 30.0,
    width: int = 1280,
    crf: int = 23,
    inline_video_max_mb: int = 20,
) -> Any:
    """
    Render a short preview clip from a source video.

    Args:
        input_path: Source video path.
        output_path: Output mp4 path.
        start: Start time in seconds, MM:SS, or HH:MM:SS.mmm.
        duration: Preview duration in seconds.
        width: Output width. Set 0 to keep original size.
        crf: H.264 CRF quality, lower is better/larger.
        inline_video_max_mb: Maximum output size to attach as ToolOutputFileContent.

    Returns:
        JSON summary plus ToolOutputFileContent when the preview is small enough.
    """
    try:
        ffmpeg = which("ffmpeg")
        source = resolve_path(input_path, must_exist=True)
        output = resolve_path(output_path)
        ensure_parent(output)

        start_seconds = parse_time(start)
        duration = max(0.1, min(float(duration), 3600.0))
        crf = max(0, min(int(crf), 35))
        args = [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-ss",
            str(start_seconds),
            "-t",
            str(duration),
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
        ]
        if int(width) > 0:
            args.extend(["-vf", f"scale={max(160, min(int(width), 3840))}:-2"])
        args.append(str(output))

        result = run_process(args, timeout=600)
        require_success(result, "preview render")
        summary = {
            "output": display_path(output),
            "input": display_path(source),
            "start": round(start_seconds, 3),
            "duration": duration,
            "size_bytes": output.stat().st_size if output.exists() else None,
            "inline_video": False,
        }
        video_output = make_video_output(output, max_inline_mb=inline_video_max_mb)
        if video_output is None:
            summary["inline_note"] = "Rendered video is available by path but was not attached as ToolOutputFileContent because it is too large."
            return _ok_json(summary)
        summary["inline_video"] = True
        return [ToolOutputText(text=_ok_json(summary)), video_output]
    except Exception as exc:
        return _error(exc)


@function_tool
def video_write_srt(captions_json: str, output_path: str = "edits/captions.srt") -> str:
    """
    Write captions/subtitles to an SRT sidecar file.

    Args:
        captions_json: JSON string or path. Accepts captions[] or a direct array with start/end/text.
        output_path: Destination .srt path.

    Returns:
        JSON summary with output path and caption count.
    """
    try:
        payload = read_json_arg(captions_json, default_name="captions_json")
        captions = payload.get("captions") if isinstance(payload, dict) else payload
        if not isinstance(captions, list):
            raise VideoToolError("captions_json must contain a captions array or be an array.")

        blocks = []
        for index, item in enumerate(captions, start=1):
            if not isinstance(item, dict):
                raise VideoToolError(f"Caption #{index} must be an object.")
            start = parse_time(item.get("start", item.get("from", "")))
            end = parse_time(item.get("end", item.get("to", "")))
            text = str(item.get("text") or item.get("caption") or "").strip()
            if end <= start:
                raise VideoToolError(f"Caption #{index} end must be after start.")
            if not text:
                raise VideoToolError(f"Caption #{index} text is empty.")
            blocks.append(
                f"{index}\n{format_srt_time(start)} --> {format_srt_time(end)}\n{text}\n"
            )

        output = resolve_path(output_path)
        ensure_parent(output)
        output.write_text("\n".join(blocks), encoding="utf-8")
        return _ok_json({"output": display_path(output), "caption_count": len(blocks)})
    except Exception as exc:
        return _error(exc)


def _concat_file_line(path: Path) -> str:
    text = path.resolve().as_posix().replace("'", "'\\''")
    return f"file '{text}'"


@function_tool
def video_render_cutlist(
    cutlist_path: str,
    output_path: str = "renders/final.mp4",
    input_path: str = "",
    width: int = 0,
    crf: int = 20,
    include_audio: bool = True,
    keep_temp: bool = False,
    inline_video_max_mb: int = 20,
) -> Any:
    """
    Render a final MP4 from a cutlist by extracting each segment and concatenating clips.

    Args:
        cutlist_path: Path to a cutlist JSON created by video_build_cutlist.
        output_path: Final MP4 output path.
        input_path: Optional source override. Defaults to cutlist.input.
        width: Output width. Set 0 to keep original size.
        crf: H.264 CRF quality for segment renders, lower is better/larger.
        include_audio: Preserve audio when present.
        keep_temp: Keep intermediate segment files for debugging.
        inline_video_max_mb: Maximum output size to attach as ToolOutputFileContent.

    Returns:
        JSON summary plus ToolOutputFileContent when the render is small enough.
    """
    try:
        cutlist_file = resolve_path(cutlist_path, must_exist=True)
        cutlist = json.loads(cutlist_file.read_text(encoding="utf-8"))
        raw_segments = cutlist.get("segments")
        if not isinstance(raw_segments, list) or not raw_segments:
            raise VideoToolError("Cutlist has no segments.")

        default_source = input_path.strip() or cutlist.get("input")
        segments = [
            _normalize_segment(raw, index, default_source) for index, raw in enumerate(raw_segments, start=1)
        ]
        summary, video_output = _render_segments(
            segments,
            output_path,
            width=width,
            crf=crf,
            include_audio=include_audio,
            keep_temp=keep_temp,
            inline_video_max_mb=inline_video_max_mb,
        )
        summary["cutlist"] = display_path(cutlist_file)
        if video_output is None:
            return _ok_json(summary)
        return [ToolOutputText(text=_ok_json(summary)), video_output]
    except Exception as exc:
        return _error(exc)


def _render_segments(
    segments: list[dict[str, Any]],
    output_path: str,
    *,
    width: int = 0,
    crf: int = 20,
    include_audio: bool = True,
    keep_temp: bool = False,
    inline_video_max_mb: int = 20,
) -> tuple[dict[str, Any], Any | None]:
    """Cut each normalized segment from its own source, then concatenate the parts."""
    ffmpeg = which("ffmpeg")
    output = resolve_path(output_path)
    ensure_parent(output)
    temp_dir = output.parent / f".{output.stem}_parts_{int(time.time())}"
    temp_dir.mkdir(parents=True, exist_ok=True)
    list_file = temp_dir / "concat.txt"

    crf = max(0, min(int(crf), 35))
    width = max(0, min(int(width), 3840))
    rendered: list[Path] = []

    for index, segment in enumerate(segments, start=1):
        source = resolve_path(segment["source"], must_exist=True)
        clip = temp_dir / f"{index:03d}_{clean_filename(segment['id'])}.mp4"
        whole_file = (
            segment["start"] <= 0.001
            and segment["end"] >= _source_duration(source) - 0.05
            and width == 0
            and include_audio
        )
        args = [ffmpeg, "-hide_banner", "-y"]
        if not whole_file:
            args.extend(["-ss", str(segment["start"]), "-to", str(segment["end"])])
        args.extend(["-i", str(source), "-map", "0:v:0"])
        if include_audio:
            args.extend(["-map", "0:a?"])
        else:
            args.append("-an")
        if whole_file:
            # Nothing to trim or rescale: keep the original streams instead of re-encoding.
            args.extend(["-c", "copy"])
        else:
            args.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"])
            if width > 0:
                args.extend(["-vf", f"scale={width}:-2"])
            if include_audio:
                args.extend(["-c:a", "aac", "-b:a", "160k"])
        args.extend(["-movflags", "+faststart", str(clip)])
        result = run_process(args, timeout=1800)
        require_success(result, f"render segment {index}")
        rendered.append(clip)

    list_file.write_text("\n".join(_concat_file_line(path) for path in rendered), encoding="utf-8")
    result = run_process(
        [
            ffmpeg,
            "-hide_banner",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        timeout=1800,
    )
    require_success(result, "concat render")

    if not keep_temp:
        shutil.rmtree(temp_dir, ignore_errors=True)

    summary = {
        "output": display_path(output),
        "inputs": list(dict.fromkeys(segment["source"] for segment in segments)),
        "segment_count": len(segments),
        "total_duration": round(sum(segment["duration"] for segment in segments), 3),
        "size_bytes": output.stat().st_size if output.exists() else None,
        "inline_video": False,
    }
    video_output = make_video_output(output, max_inline_mb=inline_video_max_mb)
    if video_output is None:
        summary["inline_note"] = "Rendered video is available by path but was not attached as ToolOutputFileContent because it is too large."
        return summary, None
    summary["inline_video"] = True
    return summary, video_output


@function_tool
def video_concat(
    inputs_json: str,
    output_path: str = "renders/merged.mp4",
    include_audio: bool = True,
    crf: int = 20,
    width: int = 0,
    inline_video_max_mb: int = 20,
) -> Any:
    """
    Join several video files end to end into one MP4, in the given order.

    Whole files with matching format are copied without re-encoding, so this is fast
    and lossless. For partial ranges, build a cutlist instead.

    Args:
        inputs_json: JSON array of video paths, or {"inputs": [...]}. Items may also be
            objects with source/start/end to take only a part of a file.
        output_path: Final MP4 output path.
        include_audio: Preserve audio when present.
        crf: H.264 quality used only when a segment must be re-encoded.
        width: Output width. Set 0 to keep the original size.
        inline_video_max_mb: Maximum output size to attach as ToolOutputFileContent.

    Returns:
        JSON summary with the output path, inputs and total duration.
    """
    try:
        payload = read_json_arg(inputs_json, default_name="inputs_json")
        if isinstance(payload, dict):
            payload = payload.get("inputs") or payload.get("segments") or payload.get("files")
        if not isinstance(payload, list) or not payload:
            raise VideoToolError('Provide a JSON array of video paths, e.g. ["a.mp4", "b.mp4"].')

        items = [{"source": item} if isinstance(item, str) else item for item in payload]
        segments = [_normalize_segment(item, index) for index, item in enumerate(items, start=1)]
        summary, video_output = _render_segments(
            segments,
            output_path,
            width=width,
            crf=crf,
            include_audio=include_audio,
            inline_video_max_mb=inline_video_max_mb,
        )
        if video_output is None:
            return _ok_json(summary)
        return [ToolOutputText(text=_ok_json(summary)), video_output]
    except Exception as exc:
        return _error(exc)


@function_tool
def video_export_review_report(
    annotation_or_cutlist_path: str,
    output_path: str = "annotations/review_report.md",
) -> str:
    """
    Convert an annotation or cutlist JSON file into a compact Markdown review report.

    Args:
        annotation_or_cutlist_path: Workspace path to annotation/cutlist JSON.
        output_path: Markdown output path.

    Returns:
        JSON summary with report path.
    """
    try:
        source_file = resolve_path(annotation_or_cutlist_path, must_exist=True)
        payload = json.loads(source_file.read_text(encoding="utf-8"))
        output = resolve_path(output_path)
        ensure_parent(output)

        title = payload.get("title") or payload.get("schema") or source_file.stem
        media = payload.get("media") or {}
        segments = payload.get("segments") or payload.get("shots") or []
        lines = [
            f"# {title}",
            "",
            f"- Source file: `{display_path(source_file)}`",
            f"- Input video: `{payload.get('input', '')}`",
        ]
        if media:
            lines.append(f"- Duration: `{media.get('duration_timecode') or media.get('duration_seconds')}`")
            lines.append(f"- Frame size: `{media.get('width')}x{media.get('height')}`")
        if payload.get("total_duration") is not None:
            lines.append(f"- Edit duration: `{format_time(float(payload['total_duration']))}`")

        lines.extend(["", "## Segments", ""])
        if segments:
            lines.append("| # | Time | Duration | Label / notes |")
            lines.append("|---:|---|---:|---|")
            for index, item in enumerate(segments, start=1):
                start = item.get("start_timecode") or format_time(parse_time(item.get("start", 0)))
                end = item.get("end_timecode") or format_time(parse_time(item.get("end", 0)))
                duration = item.get("duration")
                label = str(item.get("label") or item.get("id") or "").replace("|", "\\|")
                notes = str(item.get("notes") or "").replace("|", "\\|")
                text = label if not notes else f"{label}: {notes}"
                lines.append(f"| {index} | `{start} - {end}` | `{duration}` | {text} |")
        else:
            lines.append("No segments found.")

        output.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return _ok_json({"output": display_path(output), "source": display_path(source_file)})
    except Exception as exc:
        return _error(exc)
