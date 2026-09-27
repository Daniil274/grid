# Video Editor Agent System

Agentic example for video annotation and editing inside GRID. It adds a local toolset around FFmpeg/FFprobe and a small multi-agent workflow:

- `video_director` coordinates the task.
- `video_annotator` probes media, samples frames, detects scenes, and writes annotation JSON.
- `video_editor` builds cutlists, previews, SRT files, and final renders.
- `video_qc` checks rendered outputs before handoff.

## Requirements

Install FFmpeg and ensure both `ffmpeg` and `ffprobe` are available on `PATH`.

The Python dependencies are inherited from the root project; no extra package is required for this example.

## Layout

```text
examples/video-editor/
  config.yaml
  skills/video_pipeline.md
  tools/
    _video_common.py
    video_info_tools.py
    video_edit_tools.py
  workspace/
    inputs/        # put source videos here
    frames/        # sampled stills and contact sheets
    annotations/   # scene detection, annotation JSON, reports
    edits/         # cutlists and SRT sidecars
    renders/       # previews and final videos
```

## Run

From the repository root:

```powershell
$env:PYTHONPATH = "."
python agent_chat.py --config examples/video-editor/config.yaml --message "Probe inputs/source.mp4, create an annotation template, and make a 20 second preview."
```

Interactive:

```powershell
$env:PYTHONPATH = "."
python agent_chat.py --config examples/video-editor/config.yaml
```

## Useful prompts

```text
Analyze inputs/interview.mp4, detect scenes, sample frames every 8 seconds, and create annotations/interview_annotation.json.
```

```text
Create a 60 second highlight reel from inputs/event.mp4. Keep the strongest visual moments, render a preview first, then render the final MP4.
```

```text
Using edits/cutlist.json, render the final video to renders/final.mp4 and run QC.
```

## Tool formats

`video_create_annotation_template` creates a JSON file with `shots[]` and `recommended_cutlist.segments[]`.

`video_build_cutlist` accepts any of:

- a direct JSON array of `{start, end, label, notes}` segments;
- a JSON object with `segments[]`;
- an annotation object with `recommended_cutlist.segments[]`;
- an annotation object with `shots[]` where selected shots have `"include": true`.

`video_render_cutlist` renders each segment with H.264/AAC and concatenates the result into a final MP4.
