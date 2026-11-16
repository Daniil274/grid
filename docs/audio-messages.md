# Audio messages

The chat WebSocket supports sending recorded audio as a user message via `ChatConnection.sendAudio(dataUrl, selection)`. It sends `{message: "", audio: dataUrl, system_key, agent_key}`; the data URL is payload only and must not be rendered as chat text. The server owns transcription routing: use free, local `faster-whisper` for ordinary agents (automatic route to ASR first), and bypass ASR for a selected audio-native agent. No paid speech API is required.

This is audio input, not generated audio output or real-time streaming. Current recorder limits apply: at most 60 seconds and 4 MB. Existing continuous voice/TTS mode remains a separate STT → voice-command flow.

## Frontend integration contract

The WebSocket class exposes `sendAudio(audioDataUrl, {system_key, agent_key})`; audio capture should use `VoiceUI.encodeWav(samples, sampleRate)` and convert the resulting WAV blob to a base64 data URL. To render a non-text user bubble without exposing base64, the chat rendering path needs to retain the audio attachment metadata and display a small audio-message indicator. The current main.js/session hooks are outside the permitted edit scope, so that bubble/capture wiring remains an integration requirement.

Audio capture UI must respect `/api/voice/status` disabled/unavailable state and surface permission, limit, and send errors. Do not silently fall back to browser/cloud transcription.

## Current integration gap

The existing `voice.js` recorder is exclusively wired to `/api/voice/transcribe` for continuous voice mode. A small `main.js` hook is required to pass the current WebSocket send callback and `{system_key, agent_key}` selection into the recorder, plus an audio-specific render path to create a user bubble. Neither path is changed here because the requested edit boundary excludes main.js/session and message rendering files.

Verification: `ChatConnection.sendAudio` is covered by the dedicated audio message test.

**Backend coordination note:** expected frame shape is `{message: "", audio: "data:audio/wav;base64,...", system_key, agent_key}`. Keep audio bytes out of visible text/transcript content.
