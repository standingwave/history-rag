/* Voice capture, client half (wip/SPEC-voice-capture.md): raw PCM via
   Web Audio, downsampled to 16 kHz mono and WAV-encoded — iOS
   MediaRecorder emits AAC-in-mp4, which the model endpoint doesn't
   accept. The pure helpers are exported for tests; record() owns the
   mic and must be called from a user gesture (iOS). */

export const RATE = 16000;

export function downsample(chunks: Float32Array[], from: number, to = RATE): Float32Array {
  const total = chunks.reduce((n, c) => n + c.length, 0);
  const all = new Float32Array(total);
  let off = 0;
  for (const c of chunks) { all.set(c, off); off += c.length; }
  if (from <= to) return all;
  const ratio = from / to;
  const out = new Float32Array(Math.floor(total / ratio));
  for (let i = 0; i < out.length; i++) {
    const a = Math.floor(i * ratio), b = Math.min(total, Math.floor((i + 1) * ratio));
    let s = 0;
    for (let j = a; j < b; j++) s += all[j];
    out[i] = b > a ? s / (b - a) : 0;
  }
  return out;
}

export function wavEncode(pcm: Float32Array, rate = RATE): Uint8Array {
  const dv = new DataView(new ArrayBuffer(44 + pcm.length * 2));
  const str = (o: number, s: string) => { for (let i = 0; i < s.length; i++) dv.setUint8(o + i, s.charCodeAt(i)); };
  str(0, "RIFF"); dv.setUint32(4, 36 + pcm.length * 2, true); str(8, "WAVE");
  str(12, "fmt "); dv.setUint32(16, 16, true);
  dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);            // PCM, mono
  dv.setUint32(24, rate, true); dv.setUint32(28, rate * 2, true);  // byte rate
  dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);           // block, bits
  str(36, "data"); dv.setUint32(40, pcm.length * 2, true);
  for (let i = 0; i < pcm.length; i++) {
    const s = Math.max(-1, Math.min(1, pcm[i]));
    dv.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return new Uint8Array(dv.buffer);
}

export function b64(bytes: Uint8Array): string {
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  return btoa(s);
}

export type Recorder = { stop(): Promise<string | null>; cancel(): void };

/* Collect mic PCM until stop(); the tick reports whole seconds so the
   UI can show elapsed time and auto-submit at its cap. stop() resolves
   to base64 WAV, or null when under half a second was captured. */
export async function record(onTick: (sec: number) => void, maxSec = 30): Promise<Recorder> {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const ctx = new AudioContext();
  await ctx.resume();
  const src = ctx.createMediaStreamSource(stream);
  const proc = ctx.createScriptProcessor(4096, 1, 1);
  const chunks: Float32Array[] = [];
  let sec = 0, done = false;
  proc.onaudioprocess = (e) => {
    if (done) return;
    chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    const s = Math.floor((chunks.length * 4096) / ctx.sampleRate);
    if (s !== sec) { sec = s; onTick(s); }
    if (s >= maxSec) done = true;
  };
  src.connect(proc);
  proc.connect(ctx.destination);   // ScriptProcessor needs a sink to fire; output stays silent
  const teardown = () => {
    done = true;
    proc.disconnect(); src.disconnect();
    stream.getTracks().forEach((t) => t.stop());
    void ctx.close().catch(() => undefined);
  };
  return {
    cancel: teardown,
    async stop() {
      teardown();
      const pcm = downsample(chunks, ctx.sampleRate);
      return pcm.length >= RATE / 2 ? b64(wavEncode(pcm)) : null;
    },
  };
}
