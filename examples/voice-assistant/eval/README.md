# Russian voice evaluation corpus

This directory contains synthetic Russian utterances for regression testing of
speech recognition, endpointing and Jev floor-control decisions. It is an eval
set, not training data.

- `utterances.jsonl`: 36 references in six balanced sets.
- `profiles.json`: reproducible acoustic stress profiles.
- `generate.py`: OpenRouter TTS download, WAV normalization and augmentation.
- `audio/manifest.jsonl`: generated file metadata and exact expected text.

The default source is the free Fish Audio S2.1 endpoint. `--control-grok` also
generates the six `floor_control` anchors with five Grok voices. Grok currently
costs $15 per million input characters; the script prints an upper-bound estimate
before making paid requests. Existing valid files are reused.

```powershell
.venv\Scripts\python.exe examples\voice-assistant\eval\generate.py
.venv\Scripts\python.exe examples\voice-assistant\eval\generate.py --control-grok
.venv\Scripts\python.exe examples\voice-assistant\eval\evaluate_stt.py --profile clean
```

`OPENROUTER_API_KEY` is loaded from the repository `.env`. Requires `ffmpeg` and
`ffprobe`. Requests retry temporary errors and validate content type before saving.
Synthetic speech does not replace recordings from the actual microphone: use it
for repeatable comparisons, then validate finalists on real speakers and room echo.
The evaluator writes per-file hypotheses and prints aggregate WER/CER by acoustic
profile, utterance set and synthesis engine.
