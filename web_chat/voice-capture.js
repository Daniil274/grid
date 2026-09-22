/* Audio render-thread capture: every frame, including silence, at native rate. */
class GridVoiceCapture extends AudioWorkletProcessor {
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (channel) {
      const samples = new Float32Array(channel);
      this.port.postMessage(samples, [samples.buffer]);
    }
    return true;
  }
}
registerProcessor("grid-voice-capture", GridVoiceCapture);
