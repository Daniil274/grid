const MAX_SECONDS = 55;
const TARGET_RATE = 16000;

export function encodeWav(chunks, sampleRate, targetRate = TARGET_RATE) {
  const sourceLength = chunks.reduce((n, chunk) => n + chunk.length, 0);
  if (!Number.isFinite(sampleRate) || sampleRate <= 0 || !Number.isFinite(targetRate) || targetRate <= 0) throw new TypeError('Invalid audio sample rate');
  const input = new Float32Array(sourceLength);
  let offset = 0;
  for (const chunk of chunks) { input.set(chunk, offset); offset += chunk.length; }
  const outputLength = Math.floor(sourceLength * targetRate / sampleRate);
  const buffer = new ArrayBuffer(44 + outputLength * 2);
  const view = new DataView(buffer);
  const text = (at, value) => { for (let i = 0; i < value.length; i++) view.setUint8(at + i, value.charCodeAt(i)); };
  text(0, 'RIFF'); view.setUint32(4, buffer.byteLength - 8, true); text(8, 'WAVE'); text(12, 'fmt ');
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, targetRate, true); view.setUint32(28, targetRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, 'data'); view.setUint32(40, outputLength * 2, true);
  for (let i = 0; i < outputLength; i++) {
    const position = i * sampleRate / targetRate;
    const left = Math.floor(position), fraction = position - left;
    const value = Math.max(-1, Math.min(1, input[left] * (1 - fraction) + (input[Math.min(left + 1, sourceLength - 1)] ?? 0) * fraction));
    view.setInt16(44 + i * 2, value < 0 ? value * 32768 : value * 32767, true);
  }
  return buffer;
}

export function wavDataUrl(buffer) {
  let binary = '';
  const bytes = new Uint8Array(buffer);
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  const encode = globalThis.btoa ?? ((value) => Buffer.from(value, 'binary').toString('base64'));
  return `data:audio/wav;base64,${encode(binary)}`;
}

export function createAudioRecorder({ onSend, onState = () => {}, onError = () => {}, getUserMedia = (...args) => navigator.mediaDevices.getUserMedia(...args), AudioContextClass = globalThis.AudioContext } = {}) {
  let recording = false, epoch = 0, chunks = [], stream = null, context = null, source = null, worklet = null, zeroGain = null, timer = null;
  const state = (value) => { recording = value; onState(value); };
  function cleanup() {
    clearTimeout(timer); timer = null;
    if (worklet?.port) worklet.port.onmessage = null;
    for (const node of [source, worklet, zeroGain]) { try { node?.disconnect(); } catch {} }
    source = worklet = zeroGain = null;
    if (stream) { for (const track of stream.getTracks()) { try { track.stop(); } catch {} } stream = null; }
    const oldContext = context; context = null;
    if (oldContext) { try { Promise.resolve(oldContext.close()).catch(() => {}); } catch {} }
  }
  function cancel() { epoch++; chunks = []; cleanup(); state(false); }
  async function start() {
    if (recording) { await stop(); return; }
    const id = ++epoch; chunks = []; state(true);
    try {
      const pendingStream = await getUserMedia({ audio: true });
      if (id !== epoch) { pendingStream.getTracks().forEach((track) => track.stop()); return; }
      stream = pendingStream;
      context = new AudioContextClass();
      await context.audioWorklet.addModule('/voice-capture.js');
      if (id !== epoch) return;
      source = context.createMediaStreamSource(stream);
      worklet = new AudioWorkletNode(context, 'grid-voice-capture');
      zeroGain = context.createGain(); zeroGain.gain.value = 0;
      worklet.port.onmessage = (event) => { if (recording && id === epoch && event.data?.length) chunks.push(new Float32Array(event.data)); };
      source.connect(worklet); worklet.connect(zeroGain); zeroGain.connect(context.destination);
      timer = setTimeout(() => { if (id === epoch) void stop(); }, MAX_SECONDS * 1000);
    } catch (error) {
      if (id === epoch) { chunks = []; cleanup(); state(false); onError(error); }
    }
  }
  async function stop() {
    if (!recording) return;
    epoch++; // Also invalidate a microphone permission request still pending.
    const data = chunks, sampleRate = context?.sampleRate ?? TARGET_RATE; chunks = []; cleanup(); state(false);
    try {
      if (!data.length) throw new Error('No audio was captured');
      const url = wavDataUrl(encodeWav(data, sampleRate));
      await onSend?.(url);
    } catch (error) { onError(error); }
  }
  return { start, stop, cancel, get recording() { return recording; } };
}
