/* Local utterance capture and playback. No browser/cloud speech APIs. */
(function (root) {
  "use strict";
  function resample(samples, inputRate, outputRate = 16000) {
    if (inputRate === outputRate) return samples;
    const ratio = inputRate / outputRate;
    const result = new Float32Array(Math.floor(samples.length / ratio));
    for (let i = 0; i < result.length; i++) {
      const begin = i * ratio, end = Math.min((i + 1) * ratio, samples.length);
      let sum = 0;
      for (let j = Math.floor(begin); j < Math.ceil(end); j++) {
        sum += samples[j] * (Math.min(j + 1, end) - Math.max(j, begin));
      }
      result[i] = sum / (end - begin);
    }
    return result;
  }
  function encodeWav(samples, inputRate = 16000) {
    const pcm = resample(samples, inputRate);
    const buffer = new ArrayBuffer(44 + pcm.length * 2), view = new DataView(buffer);
    const str = (offset, text) => [...text].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
    str(0, "RIFF"); view.setUint32(4, buffer.byteLength - 8, true); str(8, "WAVE");
    str(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
    view.setUint16(22, 1, true); view.setUint32(24, 16000, true);
    view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    str(36, "data"); view.setUint32(40, pcm.length * 2, true);
    pcm.forEach((value, i) => {
      const s = Number.isFinite(value) ? Math.max(-1, Math.min(1, value)) : 0;
      view.setInt16(44 + i * 2, Math.round(s * (s < 0 ? 32768 : 32767)), true);
    });
    return buffer;
  }
  function speechText(text) {
    return String(text || "")
      .replace(/```[\s\S]*?(?:```|$)/g, " ")
      .replace(/!\[[^\]]*\]\([^)]*\)/g, " ")
      .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
      .replace(/https?:\/\/\S+/g, " ")
      .replace(/(?:Context ID|Контекст ID):\s*ctx-[\w-]+/gi, " ")
      .replace(/<[^>]*>/g, " ").replace(/[`*_#>|~]/g, " ")
      .replace(/\s+/g, " ").trim();
  }
  function splitIntoChunks(text, limit = 500) {
    const chunks = [];
    let rest = speechText(text);
    while (rest.length > limit) {
      const head = rest.slice(0, limit + 1);
      let end = -1;
      for (const match of head.matchAll(/[.!?…]\s/g)) end = match.index + 1;
      if (end < limit / 3) end = head.lastIndexOf(" ", limit);
      if (end <= 0) end = limit;
      chunks.push(rest.slice(0, end).trim()); rest = rest.slice(end).trim();
    }
    if (rest) chunks.push(rest);
    return chunks;
  }
  class Generation {
    constructor() { this.value = 0; }
    next() { return ++this.value; }
    is(value) { return this.value === value; }
  }
  class TurnDetector {
    constructor(rate) { this.rate = rate; this.reset(); this.noise = .002; }
    reset() { this.chunks = []; this.pre = []; this.preCount = 0; this.count = 0; this.active = false; this.voiced = 0; this.quiet = 0; }
    push(data, speaking = false) {
      const chunk = new Float32Array(data);
      let squares = 0;
      for (const value of chunk) squares += value * value;
      this.level = Math.sqrt(squares / (chunk.length || 1));
      const loud = this.level > Math.max(speaking ? .025 : .009, this.noise * 3.2);
      const ms = chunk.length / this.rate * 1000;
      if (!this.active) {
        this.pre.push(chunk); this.preCount += chunk.length;
        while (this.preCount > this.rate * .3 && this.pre.length > 1) this.preCount -= this.pre.shift().length;
        this.voiced = loud ? this.voiced + ms : 0;
        if (!loud) this.noise = this.noise * .995 + this.level * .005;
        if (this.voiced < 160) return null;
        this.active = true; this.chunks = this.pre; this.count = this.preCount; this.pre = [];
        this.quiet = 0;
        return {type: "start"};
      }
      this.chunks.push(chunk); this.count += chunk.length;
      this.quiet = loud ? 0 : this.quiet + ms;
      if (this.quiet < 750 && this.count < this.rate * 55) return null;
      const samples = new Float32Array(this.count);
      let offset = 0;
      for (const part of this.chunks) { samples.set(part, offset); offset += part.length; }
      this.reset();
      return {type: "end", samples};
    }
  }
  function createVoiceUI() {
    const captureGeneration = new Generation(), speechGeneration = new Generation();
    let hooks, els, phase = "ready", recording = null, transcribeAbort = null;
    let synthAbort = null, playback = null, playbackDone = null, audioUrl = null;
    // Playback belongs to the chat's SpeechPlayer when the page installs one
    // (root.GridSpeech): one owner means one visible stop button per answer.
    // Standalone (the node test sandbox) this module plays its own audio.
    const speaker = {
      get shared() { return root.GridSpeech || null; },
      get present() { return this.shared ? this.shared.activeId !== null : Boolean(playback); },
      pause() { this.shared ? this.shared.pause() : playback?.pause(); },
      resume() {
        if (this.shared) this.shared.resume();
        else playback?.play().catch(error => status("error", error.message));
      },
    };
    let voiceStatus = null, timer = null, enabled = false;
    let audioQueue = [], textQueue = [], processing = false;
    let pendingSpeech = "", decisionRevision = 0, decisionAbort = null, starting = false, lastAssistantText = "", floorHeld = false;
    function status(value, message) {
      phase = value;
      if (!els) return;
      els.status.textContent = message; els.status.dataset.phase = phase;
      els.mic.textContent = enabled ? "Выключить микрофон" : "Начать разговор";
      els.mic.setAttribute("aria-pressed", String(enabled));
      els.mic.classList.toggle("active", enabled);
    }
    async function responseError(res) {
      const data = await res.json().catch(() => null);
      return new Error(typeof data?.detail === "string" ? data.detail : `Сервис речи: HTTP ${res.status}`);
    }
    function closeCapture(session) {
      if (!session) return;
      session.stream?.getTracks().forEach(t => t.stop());
      if (session.node?.port) session.node.port.onmessage = null;
      if (session.node) session.node.onaudioprocess = null;
      [session.source, session.node, session.sink].forEach(n => { try { n?.disconnect(); } catch {} });
      session.context?.close().catch(() => {});
    }
    function resetMeter() {
      clearInterval(timer); timer = null;
      if (els) { els.level.style.width = "0%"; els.time.textContent = "00:00"; }
    }
    function stopSpeaking() {
      root.GridSpeech?.stop();
      speechGeneration.next(); synthAbort?.abort(); synthAbort = null;
      if (playback) { playback.pause(); playback.removeAttribute("src"); playback.load(); }
      playbackDone?.(); playbackDone = null; playback = null;
      if (audioUrl) URL.revokeObjectURL(audioUrl);
      audioUrl = null;
    }
    function cleanup() {
      enabled = false; audioQueue = []; textQueue = []; pendingSpeech = "";
      floorHeld = false; lastAssistantText = "";
      decisionRevision++; decisionAbort?.abort();
      captureGeneration.next(); transcribeAbort?.abort(); transcribeAbort = null;
      closeCapture(recording); recording = null;
      stopSpeaking(); resetMeter();
      status("ready", "Микрофон выключен");
    }
    async function checkStatus() {
      try {
        const res = await fetch("/api/voice/status");
        if (!res.ok) throw await responseError(res);
        voiceStatus = await res.json();
        if (els) {
          els.details.textContent = voiceStatus.issues?.join(" · ") ||
            `Распознавание: ${voiceStatus.stt_model} · ${voiceStatus.stt_device}. Звук обрабатывается на этом компьютере.`;
          els.mic.disabled = !voiceStatus.enabled || !voiceStatus.stt_available;
        }
      } catch (error) { if (els) els.details.textContent = error.message; }
    }
    async function startRecording() {
      if (starting || enabled) return;
      starting = true;
      try { if (!hooks.getContextId()) await hooks.ensureConversation(); }
      catch (error) { status("error", error.message); starting = false; return; }
      starting = false;
      enabled = true;
      stopSpeaking(); transcribeAbort?.abort();
      const generation = captureGeneration.next();
      const session = {contextId: hooks.getContextId(), draft: hooks.getDraft(), chunks: [], count: 0, energy: 0, peak: 0};
      recording = session;
      status("acquiring", "Разрешите доступ к микрофону");
      try {
        if (!navigator.mediaDevices?.getUserMedia) throw new Error("Микрофон доступен через localhost или HTTPS.");
        session.stream = await navigator.mediaDevices.getUserMedia({audio: {
          echoCancellation: true, noiseSuppression: true, channelCount: 1,
        }});
        if (!captureGeneration.is(generation)) { closeCapture(session); return; }
        session.context = new (root.AudioContext || root.webkitAudioContext)();
        session.detector = new TurnDetector(session.context.sampleRate);
        await session.context.resume();
        if (!captureGeneration.is(generation)) { closeCapture(session); return; }
        const receive = (data) => {
          if (!captureGeneration.is(generation) || !enabled) return;
          const event = session.detector.push(data, speaker.present);
          session.energy = session.detector.level;
          if (event?.type === "start") {
            decisionRevision++; decisionAbort?.abort();
            floorHeld = true;
            speaker.pause();
            status("listening", "Слушаю вас…");
          } else if (event?.type === "end") {
            if (audioQueue.length < 3) {
              audioQueue.push({samples: event.samples, rate: session.context.sampleRate, generation, contextId: session.contextId});
              void processAudio();
            } else status("error", "Распознавание не успевает. Подождите немного.");
          }
        };
        if (session.context.audioWorklet && root.AudioWorkletNode) {
          await session.context.audioWorklet.addModule("/static/voice-capture.js");
          if (!captureGeneration.is(generation)) { closeCapture(session); return; }
          session.node = new root.AudioWorkletNode(session.context, "grid-voice-capture");
          session.node.port.onmessage = e => receive(e.data);
        } else {
          session.node = session.context.createScriptProcessor(4096, 1, 1);
          session.node.onaudioprocess = e => receive(e.inputBuffer.getChannelData(0));
        }
        session.source = session.context.createMediaStreamSource(session.stream);
        session.sink = session.context.createGain(); session.sink.gain.value = 0;
        session.source.connect(session.node); session.node.connect(session.sink);
        session.sink.connect(session.context.destination);
        session.started = performance.now();
        status("listening", "Говорите — я слушаю. Ответ можно перебить голосом.");
        timer = setInterval(() => {
          if (!recording || !enabled) return;
          const seconds = Math.floor((performance.now() - session.started) / 1000);
          els.time.textContent = `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
          els.level.style.width = `${Math.min(100, session.energy * 500)}%`;
          void sendPending();
        }, 100);
      } catch (error) {
        closeCapture(session);
        if (captureGeneration.is(generation)) {
          enabled = false; recording = null; resetMeter();
          status("error", error.name === "NotAllowedError" ? "Доступ к микрофону не разрешён." :
            error.name === "NotFoundError" ? "Микрофон не найден." : error.message);
        }
      }
    }
    async function sendPending() {
      if (!enabled || processing || hooks.isStreaming() || recording?.detector.active || !textQueue.length) return;
      const item = textQueue.shift();
      if (hooks.getContextId() !== item.contextId) return;
      await hooks.sendVoiceMessage(item.text);
    }
    async function processAudio() {
      if (processing) return;
      processing = true;
      try {
        while (enabled && audioQueue.length) {
          const item = audioQueue.shift();
          const controller = new AbortController(); transcribeAbort = controller;
          status("transcribing", "Распознаю фразу… Микрофон продолжает слушать.");
          try {
            let res;
            for (let attempt = 0; attempt < 8; attempt++) {
              res = await fetch("/api/voice/transcribe", {method: "POST",
                headers: {"Content-Type": "audio/wav"}, body: encodeWav(item.samples, item.rate), signal: controller.signal});
              if (res.status !== 429) break;
              await new Promise(resolve => setTimeout(resolve, 300));
              if (!captureGeneration.is(item.generation)) break;
            }
            if (!captureGeneration.is(item.generation)) continue;
            if (!res.ok) throw await responseError(res);
            const result = await res.json();
            if (!enabled || !captureGeneration.is(item.generation) || hooks.getContextId() !== item.contextId) continue;
            if (!result.text?.trim()) {
              if (!pendingSpeech && !recording?.detector.active && !audioQueue.length) {
                floorHeld = false;
                speaker.resume();
              }
              status("listening", "Не разобрал фразу. Повторите, я слушаю."); continue;
            }
            els.transcript.textContent = result.text;
            pendingSpeech = `${pendingSpeech} ${result.text}`.trim();
            if (pendingSpeech.length > 12000) {
              status("error", "Слишком длинная незавершённая реплика. Выключите и включите микрофон, чтобы начать заново.");
              continue;
            }
            const revision = decisionRevision;
            if (recording?.detector.active || audioQueue.length) continue;
            decisionAbort = new AbortController();
            status("deciding", "Определяю, завершена ли мысль…");
            const decisionResponse = await fetch("/api/voice/decide", {method: "POST",
              headers: {"Content-Type": "application/json"}, signal: decisionAbort.signal,
              body: JSON.stringify({text: pendingSpeech, context_id: item.contextId,
                agent_busy: hooks.isStreaming(), assistant_text: (hooks.getAssistantText() || lastAssistantText).slice(-6000)})});
            if (!decisionResponse.ok) throw await responseError(decisionResponse);
            const decision = await decisionResponse.json();
            if (!enabled || revision !== decisionRevision || !captureGeneration.is(item.generation)) continue;
            if (decision.action === "wait") {
              status("listening", "Слушаю продолжение…"); continue;
            }
            if (decision.action === "respond") stopSpeaking();
            if (decision.action === "interrupt") {
              stopSpeaking(); textQueue = [];
              if (hooks.isStreaming()) hooks.interruptAgent();
            }
            if (decision.action === "respond" || (decision.action === "interrupt" && decision.replacement)) {
              if (textQueue.length >= 8) {
                status("error", "Много ожидающих реплик; текущая мысль сохранена."); continue;
              }
              textQueue.push({text: pendingSpeech, contextId: item.contextId});
            }
            pendingSpeech = "";
            floorHeld = false;
            if (decision.action === "ignore" && speaker.present) {
              speaker.resume();
            }
            status("listening", decision.action === "ignore" ? "Слушаю…" : "Понял. Можете продолжать говорить.");
            await sendPending();
          } catch (error) {
            if (captureGeneration.is(item.generation) && error.name !== "AbortError") status("error", error.message);
          } finally { if (transcribeAbort === controller) transcribeAbort = null; }
        }
      } finally { processing = false; }
    }
    async function speak(text, contextId, expectedEpoch) {
      lastAssistantText = text;
      if (!enabled || !voiceStatus?.tts_available || (expectedEpoch !== undefined && expectedEpoch !== speechGeneration.value)) return;
      if (textQueue.length) return;
      stopSpeaking();
      if (speaker.shared) {
        status("ready", "Говорю. Можете перебить меня голосом.");
        await speaker.shared.speak(contextId || "voice", text);
        if (enabled) status("listening", "Слушаю вас…");
        return;
      }
      const generation = speechGeneration.value;
      const current = () => speechGeneration.is(generation) && hooks.getContextId() === contextId && enabled;
      try {
        for (const chunk of splitIntoChunks(text)) {
          if (!current()) return;
          status("ready", "Готовлю голосовой ответ…");
          const controller = new AbortController(); synthAbort = controller;
          let res;
          for (let attempt = 0; attempt < 8; attempt++) {
            res = await fetch("/api/voice/synthesize", {method: "POST",
              headers: {"Content-Type": "application/json"}, body: JSON.stringify({text: chunk}), signal: controller.signal});
            if (res.status !== 429) break;
            await new Promise(resolve => setTimeout(resolve, 300));
            if (!current()) return;
          }
          if (!res.ok) throw await responseError(res);
          const blob = await res.blob();
          while (floorHeld && current()) await new Promise(resolve => setTimeout(resolve, 50));
          if (!current()) return;
          synthAbort = null;
          audioUrl = URL.createObjectURL(blob); playback = new Audio(audioUrl);
          status("ready", "Говорю. Можете перебить меня голосом.");
          await new Promise((resolve, reject) => {
            const audio = playback;
            const done = () => { audio.onended = null; audio.onerror = null; resolve(); };
            playbackDone = done; audio.onended = done;
            audio.onerror = () => reject(new Error("Не удалось воспроизвести звук."));
            audio.play().catch(reject);
          });
          if (!current()) return;
          playback = null; playbackDone = null;
          URL.revokeObjectURL(audioUrl); audioUrl = null;
        }
        if (current()) status("listening", "Слушаю вас…");
      } catch (error) {
        if (current() && error.name !== "AbortError") status("error", error.message);
      } finally { if (speechGeneration.is(generation)) stopSpeaking(); }
    }
    function setup(callbacks) {
      hooks = callbacks;
      const panel = document.createElement("section"); panel.id = "voice-panel";
      panel.setAttribute("aria-label", "Голосовой помощник");
      panel.innerHTML = `
        <div class="voice-panel__header">Голосовой помощник <span>Локальная обработка звука</span></div>
        <div class="voice-panel__body">
          <button class="voice-panel__mic" id="voice-mic-btn" type="button" aria-pressed="false">Начать разговор</button>
          <div class="voice-panel__controls">
            <div class="voice-panel__row"><span class="voice-panel__time" id="voice-rec-time">00:00</span>
              <div class="voice-panel__level-bar" aria-label="Уровень микрофона"><div class="voice-panel__level-fill" id="voice-level-bar"></div></div></div>
            <div class="voice-panel__status" id="voice-status" role="status" aria-live="polite">Микрофон выключен</div>
          </div>
        </div>
        <div class="voice-panel__details" id="voice-details"></div>
        <div class="voice-panel__transcript" id="voice-transcript" aria-live="polite"></div>`;
      document.querySelector(".composerWrap").prepend(panel);
      const get = id => document.getElementById(`voice-${id}`);
      els = {mic: get("mic-btn"), status: get("status"), details: get("details"), time: get("rec-time"),
        level: get("level-bar"), transcript: get("transcript")};
      els.mic.addEventListener("click", () => {
        if (enabled) cleanup();
        else void startRecording();
      });
      root.addEventListener("pagehide", cleanup);
      void checkStatus();
    }
    return {setup, cleanup, checkStatus, speak, stopSpeaking, responseEpoch: () => speechGeneration.value};
  }
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {encodeWav, resample, splitIntoChunks, speechText, Generation, TurnDetector, createVoiceUI};
  } else root.VoiceUI = createVoiceUI();
})(typeof window !== "undefined" ? window : globalThis);
