# Local voice for Grid

The existing web chat now has continuous microphone capture and spoken answers. Audio recognition and synthesis run on this computer.
The example uses **Mercury 2.5 and Jev through OpenRouter** for conversation and
semantic turn control and agent selection: recognized text is sent to that provider while the conversation is enabled.

## Start

From the repository root on Windows:

```powershell
.venv\Scripts\python.exe -m web_chat --config examples/voice-assistant/config.yaml.example --path . --port 8000
```

Open <http://127.0.0.1:8000>. The launcher loads credentials from the repository's
`.env`; set `OPENROUTER_API_KEY` there or in the environment. Existing agent configs
also work: pass their path with `--config` and add a `voice:` section if needed.

For a new environment, install the project dependencies and the speech packages:

```powershell
python -m pip install -r requirements.txt
python -m pip install faster-whisper torch numpy soundfile
```

For CUDA, use a CUDA-enabled PyTorch installation and the CUDA/cuDNN runtime
required by your installed faster-whisper/CTranslate2 version. Models are lazy
loaded. The first Whisper call may download its selected model if not cached;
later calls use the local cache. Status checks never load or download a model.

## Conversation control

Click **Начать разговор** once to grant microphone access. The microphone stays
open until disabled or the conversation changes. There is no per-utterance stop
or send button. Spoken requests preserve any typed draft.

An acoustic detector collects speech and uses 750 ms silence as an ASR boundary,
not as permission to respond. Whisper transcribes locally. Jev receives accumulated
fragments and recent assistant text via `/api/voice/decide`, then chooses:

- `wait`: retain the unfinished thought and listen for continuation;
- `respond`: submit a complete turn (queue if the agent is busy);
- `interrupt`: cancel active execution, discard queued requests, optionally submit
  the replacement instruction that Jev identifies;
- `ignore`: discard background speech or a non-actionable acknowledgement.

New speech immediately pauses playback locally; semantic cancellation of an agent
requires Jev's decision. Stale decisions are discarded if speech resumes. A decision
error retains the text and shows an error instead of dispatching work automatically.
The next spoken fragment retries evaluation with the retained text.

Microphone access requires localhost or HTTPS. Acoustic detection currently uses
adaptive energy thresholds with browser echo cancellation, not a neural VAD.
ASR works on acoustic segments, not streaming partial tokens. TTS speaks chunks
of completed answers. If Jev classifies the speech as an acknowledgement/background (`ignore`), paused
audio resumes. Incomplete thoughts keep it paused; a new request replaces it. Noise and speaker echo require testing
with the actual microphone. Full background-agent steering remains separate from
this turn controller: additional requests wait, explicit corrections cancel.

## Models and resources

The example starts with Whisper `small`, CUDA `int8_float16`, Russian, beam size 1.
Set `voice.stt.device: cpu` and `compute_type: int8` for a CPU-only installation.
For multiple GPUs, select `voice.stt.device_index`. Silero defaults to CPU so it
does not compete with recognition for GPU memory.

Silero uses a trusted local PyTorch package at `speech-text/model.pt`. Download a
Russian voice from the [official model catalog](https://github.com/snakers4/silero-models/blob/master/models.yml).
An explicitly configured relative `voice.tts.model_path` resolves against the
configuration file's directory. The default resolves against the repository root.
Do not substitute an untrusted PyTorch package.

`voice.tts.backend` supports `auto`, `silero`, and `windows` (the example selects
`auto`; other configs keep the Silero default). In auto mode, a local
Silero model is preferred; on Windows without that file, an installed Russian
Windows voice is used offline. Windows speech adds process startup overhead and
is a fallback, not a neural streaming synthesizer. Install a Russian voice in
Windows speech settings if necessary. On other systems, install the Silero model.
The interface reports missing speech dependencies without disabling text chat.

`voice.auto_route: true` lets Jev choose between the default conversational
assistant and the engineer within this configuration. Choosing a specialist
explicitly bypasses routing. The route is remembered per conversation; routing
has a two-second deadline and falls back to the requested assistant.
Set it to `false` to remove the routing round trip.

Restart the server after changing speech settings or installing a model. The web
settings editor preserves the `voice:` section.

## Verification

```powershell
.venv\Scripts\python.exe -m pytest tests/test_web_voice.py tests/test_web_chat_session.py tests/test_web_chat_runtime.py tests/test_web_ui_routes.py tests/test_windows_speech.py tests/test_voice_turns.py -q
node --test tests/voice_client.test.cjs tests/voice_conversation.test.cjs
```

The automated tests mock inference and providers. They check malformed/truncated
WAV input, bounds, cancellation, stale UI results and the actual audio encoding
helpers. They do not measure microphone quality or guarantee a particular latency.
