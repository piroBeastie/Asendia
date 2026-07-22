// AudioWorklet: capture mic audio, downsample from the context rate to 16 kHz,
// convert to little-endian Int16 PCM, and post ~64 ms chunks to the main thread.
// `sampleRate` is a global in AudioWorkletGlobalScope (the context's rate).

const TARGET_RATE = 16000;
const FLUSH_SAMPLES = 1024; // ~64 ms at 16 kHz — small enough for snappy barge-in

class PCMWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this._step = sampleRate / TARGET_RATE; // input samples per output sample
    this._phase = 0;                        // fractional read position, carried across blocks
    this._out = [];                         // accumulated 16 kHz float samples
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel || channel.length === 0) return true;

    // Linear-interpolating resample to 16 kHz. Phase carries between 128-frame
    // blocks so there are no gaps or repeated samples at block seams.
    while (this._phase < channel.length) {
      const i = Math.floor(this._phase);
      const frac = this._phase - i;
      const s0 = channel[i];
      const s1 = i + 1 < channel.length ? channel[i + 1] : channel[i];
      this._out.push(s0 + (s1 - s0) * frac);
      this._phase += this._step;
    }
    this._phase -= channel.length;

    if (this._out.length >= FLUSH_SAMPLES) {
      const pcm = new Int16Array(this._out.length);
      for (let k = 0; k < this._out.length; k++) {
        let v = this._out[k];
        v = v < -1 ? -1 : v > 1 ? 1 : v;
        pcm[k] = v < 0 ? v * 0x8000 : v * 0x7fff;
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer]); // transfer, no copy
      this._out = [];
    }
    return true;
  }
}

registerProcessor("pcm-worklet", PCMWorklet);
