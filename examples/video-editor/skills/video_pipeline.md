# Video Pipeline Skill

Use this skill for video annotation, cutlist creation, rendering, and QC.

## Workspace contract

- Source media belongs in `inputs/`.
- Frame samples and contact sheets belong in `frames/`.
- Annotation JSON and review notes belong in `annotations/`.
- Cutlists and subtitle sidecars belong in `edits/`.
- Rendered outputs belong in `renders/`.

Do not scatter temporary files outside these directories.

## Standard workflow

1. Inspect source media with `video_probe`.
2. Sample frames with `video_sample_frames` when visual understanding matters.
3. Create scene ranges with `video_detect_scenes` or `video_create_annotation_template`.
4. Convert selected ranges into an edit decision list with `video_build_cutlist`.
5. Render a short `video_make_preview` for uncertain edits.
6. Render final output with `video_render_cutlist`.
7. Verify output with `video_probe` and, when useful, `video_sample_frames`.

## Annotation schema

Annotations should stay JSON-compatible. Prefer this shape:

```json
{
  "schema": "grid.video.annotation.v1",
  "input": "inputs/source.mp4",
  "global_notes": "Short editorial summary",
  "shots": [
    {
      "id": "shot_001",
      "start": 0.0,
      "end": 5.25,
      "notes": "Opening title",
      "include": true
    }
  ],
  "recommended_cutlist": {
    "segments": [
      {
        "start": 0.0,
        "end": 5.25,
        "label": "Opening title",
        "notes": "Keep full shot"
      }
    ]
  }
}
```

If the user asks for a highlight reel, trailer, cleanup pass, or rough cut, put the selected ranges in `recommended_cutlist.segments`.

## Cutlist schema

`video_build_cutlist` writes this shape:

```json
{
  "schema": "grid.video.cutlist.v1",
  "title": "rough cut",
  "input": "inputs/source.mp4",
  "segments": [
    {
      "id": "segment_001",
      "label": "Intro",
      "start": 0.0,
      "end": 4.5,
      "notes": "Keep"
    }
  ]
}
```

Times may be seconds, `MM:SS`, or `HH:MM:SS.mmm`.

## Quality bar

- Never invent exact timestamps. Derive them from tools or mark them as approximate.
- Preserve source order unless the user asks for rearrangement.
- Prefer previews before final renders when the edit contains judgement calls.
- Final reports must include paths to annotation, cutlist, render, and QC report when those files exist.
