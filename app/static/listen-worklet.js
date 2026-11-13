'use strict';
// Plays the station's sound for the waterfall page's Listen button, in the browser's own audio thread: samples
// arrive a tenth of a second at a time over the network (12,000 a second). It keeps about a third of a second in
// hand so the network's hiccups don't break the sound up; after a gap it waits for that much again (and keeps a
// little more in hand from then on); if more than two seconds pile up, it drops the oldest so the sound stays
// close to live. When the page runs at another rate (most do, at 48,000), it steps through the samples evenly.
class StationPlayer extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const o = (options && options.processorOptions) || {};
    this.inRate = o.inRate || 12000;
    this.step = this.inRate / sampleRate;   // input samples for each output sample: 1 when the page runs at 12,000
    this.queue = [];
    this.cur = null;
    this.pos = 0;
    this.have = 0;                          // samples waiting, the current part included
    this.playing = false;
    this.lead = Math.round(0.35 * this.inRate);
    this.most = Math.round(2.0 * this.inRate);
    this.gaps = 0;
    this.a = 0;
    this.b = 0;
    this.frac = 0;
    this.primed = false;
    this.told = 0;
    this.port.onmessage = (e) => {
      const d = e.data;
      if (d instanceof Float32Array) {
        this.queue.push(d);
        this.have += d.length;
        if (this.have > this.most) this.trim();
      } else if (d && d.flush) {
        this.queue = [];
        this.cur = null;
        this.have = 0;
        this.playing = false;
        this.primed = false;
      }
    };
  }

  trim() {   // back to the lead, dropping the oldest whole parts
    if (this.cur) { this.have -= this.cur.length - this.pos; this.cur = null; }
    while (this.queue.length > 1 && this.have - this.queue[0].length >= this.lead) this.have -= this.queue.shift().length;
  }

  next() {   // the next sample, or null when there are none
    while (!this.cur || this.pos >= this.cur.length) {
      this.cur = this.queue.shift() || null;
      this.pos = 0;
      if (!this.cur) return null;
    }
    this.have -= 1;
    return this.cur[this.pos++];
  }

  gap() {
    this.playing = false;
    this.primed = false;
    this.gaps += 1;
    this.lead = Math.min(Math.round(this.lead * 1.4), Math.round(1.2 * this.inRate));
  }

  process(inputs, outputs) {
    const out = outputs[0] && outputs[0][0];
    if (!out) return true;
    if (!this.playing && this.have >= this.lead) this.playing = true;
    for (let i = 0; i < out.length; i++) {
      if (!this.playing) { out[i] = 0; continue; }
      if (this.step === 1) {
        const v = this.next();
        if (v === null) { this.gap(); out[i] = 0; continue; }
        out[i] = v;
        continue;
      }
      if (!this.primed) {
        const a = this.next(), b = a === null ? null : this.next();
        if (b === null) { this.gap(); out[i] = 0; continue; }
        this.a = a; this.b = b; this.frac = 0; this.primed = true;
      }
      out[i] = this.a + (this.b - this.a) * this.frac;
      this.frac += this.step;
      while (this.frac >= 1) {
        const v = this.next();
        if (v === null) { this.gap(); break; }
        this.a = this.b; this.b = v; this.frac -= 1;
      }
    }
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    if (currentTime - this.told >= 0.25) {
      this.told = currentTime;
      this.port.postMessage({ have: this.have / this.inRate, gaps: this.gaps, playing: this.playing, lead: this.lead / this.inRate });
    }
    return true;
  }
}
registerProcessor('station-player', StationPlayer);
