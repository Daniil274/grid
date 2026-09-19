"""Video inspection and annotation tools for the video-editor example."""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

from agents import function_tool
from agents.tool import ToolOutputImage, ToolOutputText

from _video_common import (
    VideoToolError,
    audio_streams,
    choose_tile,
    display_path,
    duration_seconds,
    encode_image_data_uri,
    ensure_dir,
    format_time,
    probe_media,
    require_success,
    resolve_path,
    run_process,
    video_stream,
    which,
    write_json,
)


def _ok_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _error(exc: Exception) -> str:
    return f"ERROR: {exc}"


def _detect_scene_payload(
    input_path: str,
    output_path: str,
    threshold: float,
    min_gap_seconds: float,
    max_scenes: int,
) -> dict[str, Any]:
    ffmpeg = which("ffmpeg")
    media_path = resolve_path(input_path, must_exist=True)
    out_path = resolve_path(output_path)
    probe = probe_media(media_path)
    duration = duration_seconds(probe)

    threshold = max(0.05, min(float(threshold), 0.95))
    min_gap_seconds = max(0.0, float(min_gap_seconds))
    max_scenes = max(1, min(int(max_scenes), 1000))

    result = run_process(
        [
            ffmpeg,
            "-hide_banner",
            "-i",
            str(media_path),
            "-filter:v",
            f"select='gt(scene,{threshold})',showinfo",
            "-f",
            "null",
            "-",
        ],
        timeout=600,
    )
    # ffmpeg writes showinfo to stderr and exits 0 on success.
    require_success(result, "scene detection")

    times: list[float] = []
    pattern = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
    for match in pattern.finditer(result.stderr or ""):
        value = float(match.group(1))
        if times and value - times[-1] < min_gap_seconds:
            continue
        if value <= 0.05 or (duration and value >= duration):
            continue
        times.append(value)
        if len(times) >= max_scenes:
            break

    boundaries = [0.0] + times
    if duration > 0:
        boundaries.append(duration)

    shots = []
    for index in range(len(boundaries) - 1):
        start = boundaries[index]
        end = boundaries[index + 1]
        if end <= start:
            continue
        shots.append(
            {
                "id": f"shot_{index + 1:03d}",
                "start": round(start, 3),
                "end": round(end, 3),
                "start_timecode": format_time(start),
                "end_timecode": format_time(end),
                "duration": round(end - start, 3),
                "notes": "",
                "include": None,
            }
        )

    payload = {
        "input": display_path(media_path),
        "duration_seconds": duration,
        "threshold": threshold,
        "min_gap_seconds": min_gap_seconds,
        "cut_count": len(times),
        "cuts": [round(v, 3) for v in times],
        "shots": shots,
    }
    write_json(out_path, payload)
    return {"output": display_path(out_path), **payload}


@function_tool
def video_probe(input_path: str) -> str:
    """
    Inspect a video/audio file with ffprobe and return concise technical metadata.

    Args:
        input_path: Video path relative to the agent working directory.

    Returns:
        JSON summary with duration, video stream, audio streams, and container tags.
    """
    try:
        media_path = resolve_path(input_path, must_exist=True)
        probe = probe_media(media_path)
        v = video_stream(probe)
        audios = audio_streams(probe)
        summary = {
            "input": display_path(media_path),
            "duration_seconds": duration_seconds(probe),
            "duration_timecode": format_time(duration_seconds(probe)),
            "format": {
                "format_name": (probe.get("format") or {}).get("format_name"),
                "size_bytes": int(float((probe.get("format") or {}).get("size") or 0)),
                "bit_rate": (probe.get("format") or {}).get("bit_rate"),
            },
            "video": {
                "codec": v.get("codec_name"),
                "width": v.get("width"),
                "height": v.get("height"),
                "pix_fmt": v.get("pix_fmt"),
                "avg_frame_rate": v.get("avg_frame_rate"),
                "r_frame_rate": v.get("r_frame_rate"),
                "duration": v.get("duration"),
            },
            "audio": [
                {
                    "index": s.get("index"),
                    "codec": s.get("codec_name"),
                    "channels": s.get("channels"),
                    "sample_rate": s.get("sample_rate"),
                    "language": (s.get("tags") or {}).get("language"),
                }
                for s in audios
            ],
        }
        return _ok_json(summary)
    except Exception as exc:
        return _error(exc)


@function_tool
def video_sample_frames(
    input_path: str,
    output_dir: str = "frames",
    every_seconds: float = 5.0,
    max_frames: int = 60,
    width: int = 640,
    make_contact_sheet: bool = True,
) -> Any:
    """
    Extract evenly spaced review frames and optionally return a visual contact sheet.

    Args:
        input_path: Video path relative to the agent working directory.
        output_dir: Directory for extracted frames.
        every_seconds: Sampling interval in seconds.
        max_frames: Maximum number of frames to extract.
        width: Output frame width in pixels, preserving aspect ratio.
        make_contact_sheet: If true, create and return a contact sheet image.

    Returns:
        Text summary and, when possible, an inline contact sheet image.
    """
    try:
        ffmpeg = which("ffmpeg")
        media_path = resolve_path(input_path, must_exist=True)
        out_dir = resolve_path(output_dir)
        ensure_dir(out_dir)

        every_seconds = max(0.2, float(every_seconds))
        max_frames = max(1, min(int(max_frames), 240))
        width = max(160, min(int(width), 1920))

        # Use a timestamped prefix so repeated calls do not overwrite earlier frame sets.
        prefix = f"sample_{int(time.time())}"
        pattern = out_dir / f"{prefix}_%04d.jpg"
        vf = f"fps=1/{every_seconds},scale={width}:-2"
        result = run_process(
            [
                ffmpeg,
                "-hide_banner",
                "-y",
                "-i",
                str(media_path),
                "-vf",
                vf,
                "-frames:v",
                str(max_frames),
                "-q:v",
                "2",
                str(pattern),
            ],
            timeout=300,
        )
        require_success(result, "frame sampling")

        frames = sorted(out_dir.glob(f"{prefix}_*.jpg"))
        if not frames:
            raise VideoToolError("No frames were extracted.")

        frame_paths = [display_path(frame) for frame in frames]
        payload = {
            "input": display_path(media_path),
            "frames_dir": display_path(out_dir),
            "frame_count": len(frames),
            "sampling_interval_seconds": every_seconds,
            "frames": frame_paths,
        }

        contact_sheet_path: Path | None = None
        if make_contact_sheet:
            cols, rows = choose_tile(len(frames))
            contact_sheet_path = out_dir / f"{prefix}_contact_sheet.jpg"
            result = run_process(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-y",
                    "-framerate",
                    "1",
                    "-i",
                    str(pattern),
                    "-frames:v",
                    "1",
                    "-vf",
                    f"scale=320:-2,tile={cols}x{rows}:padding=8:margin=8",
                    "-q:v",
                    "3",
                    str(contact_sheet_path),
                ],
                timeout=120,
            )
            require_success(result, "contact sheet rendering")
            payload["contact_sheet"] = display_path(contact_sheet_path)

        text = _ok_json(payload)
        if contact_sheet_path and contact_sheet_path.exists():
            return [
                ToolOutputText(text=text),
                ToolOutputImage(image_url=encode_image_data_uri(contact_sheet_path)),
            ]
        return text
    except Exception as exc:
        return _error(exc)


@function_tool
def video_detect_scenes(
    input_path: str,
    output_path: str = "annotations/scenes.json",
    threshold: float = 0.35,
    min_gap_seconds: float = 1.0,
    max_scenes: int = 120,
) -> str:
    """
    Detect likely scene cuts using FFmpeg's scene score filter and write shot ranges.

    Args:
        input_path: Video path relative to the agent working directory.
        output_path: JSON output path for scene cut and shot ranges.
        threshold: Scene score threshold. Higher means fewer cuts.
        min_gap_seconds: Ignore cuts closer than this many seconds.
        max_scenes: Maximum cuts to keep.

    Returns:
        JSON summary with cut points and output path.
    """
    try:
        payload = _detect_scene_payload(
            input_path=input_path,
            output_path=output_path,
            threshold=threshold,
            min_gap_seconds=min_gap_seconds,
            max_scenes=max_scenes,
        )
        return _ok_json(payload)
    except Exception as exc:
        return _error(exc)


@function_tool
def video_create_annotation_template(
    input_path: str,
    output_path: str = "annotations/annotation.json",
    scene_threshold: float = 0.35,
) -> str:
    """
    Create a structured annotation template for a video.

    Args:
        input_path: Video path relative to the agent working directory.
        output_path: JSON output path.
        scene_threshold: Scene detection threshold used to seed shot entries.

    Returns:
        JSON summary with the annotation file path.
    """
    try:
        media_path = resolve_path(input_path, must_exist=True)
        out_path = resolve_path(output_path)
        probe = probe_media(media_path)
        duration = duration_seconds(probe)
        v = video_stream(probe)

        # Call the same scene detector logic through ffmpeg, but keep the template
        # usable even when detection fails.
        shots: list[dict[str, Any]] = []
        try:
            detected = _detect_scene_payload(
                input_path=input_path,
                output_path=str(Path(output_path).with_name(Path(output_path).stem + "_scenes.json")),
                threshold=scene_threshold,
                min_gap_seconds=1.0,
                max_scenes=120,
            )
            shots = detected.get("shots") or []
        except Exception:
            shots = []

        if not shots and duration > 0:
            segment_count = max(1, min(12, math.ceil(duration / 30)))
            segment = duration / segment_count
            for index in range(segment_count):
                start = index * segment
                end = duration if index == segment_count - 1 else (index + 1) * segment
                shots.append(
                    {
                        "id": f"segment_{index + 1:03d}",
                        "start": round(start, 3),
                        "end": round(end, 3),
                        "start_timecode": format_time(start),
                        "end_timecode": format_time(end),
                        "duration": round(end - start, 3),
                        "notes": "",
                        "include": None,
                    }
                )

        payload = {
            "schema": "grid.video.annotation.v1",
            "input": display_path(media_path),
            "created_by": "video_create_annotation_template",
            "media": {
                "duration_seconds": duration,
                "duration_timecode": format_time(duration),
                "width": v.get("width"),
                "height": v.get("height"),
                "codec": v.get("codec_name"),
            },
            "global_notes": "",
            "shots": shots,
            "moments": [],
            "recommended_cutlist": {
                "segments": [],
                "notes": "Fill with selected ranges before calling video_build_cutlist.",
            },
        }
        write_json(out_path, payload)
        return _ok_json(
            {
                "output": display_path(out_path),
                "input": display_path(media_path),
                "duration_seconds": duration,
                "shot_count": len(shots),
            }
        )
    except Exception as exc:
        return _error(exc)
