import { test } from "node:test";
import assert from "node:assert/strict";

import { downsample, wavEncode, b64, RATE } from "../src/wav.ts";

test("downsample 48k → 16k averages triples", () => {
  const chunk = new Float32Array(48000);
  chunk.fill(0.3);
  const out = downsample([chunk], 48000);
  assert.equal(out.length, 16000);
  assert.ok(Math.abs(out[0] - 0.3) < 1e-6);
  assert.ok(Math.abs(out[15999] - 0.3) < 1e-6);
});

test("downsample concatenates chunks and passes ≤ target rates through", () => {
  const a = new Float32Array([1, 2]), b = new Float32Array([3]);
  assert.deepEqual([...downsample([a, b], RATE)], [1, 2, 3]);
});

test("wav header describes 16 kHz mono PCM16", () => {
  const pcm = new Float32Array(RATE);   // one second
  const w = wavEncode(pcm);
  const dv = new DataView(w.buffer);
  const tag = (o: number, n: number) => String.fromCharCode(...w.subarray(o, o + n));
  assert.equal(w.length, 44 + RATE * 2);
  assert.equal(tag(0, 4), "RIFF");
  assert.equal(tag(8, 4), "WAVE");
  assert.equal(dv.getUint32(4, true), 36 + RATE * 2);
  assert.equal(dv.getUint16(20, true), 1);         // PCM
  assert.equal(dv.getUint16(22, true), 1);         // mono
  assert.equal(dv.getUint32(24, true), RATE);
  assert.equal(dv.getUint32(28, true), RATE * 2);  // byte rate
  assert.equal(dv.getUint16(34, true), 16);        // bits
  assert.equal(tag(36, 4), "data");
  assert.equal(dv.getUint32(40, true), RATE * 2);
});

test("samples clip to int16 range", () => {
  const w = wavEncode(new Float32Array([2, -2, 0.5]));
  const dv = new DataView(w.buffer);
  assert.equal(dv.getInt16(44, true), 0x7fff);
  assert.equal(dv.getInt16(46, true), -0x8000);
  assert.equal(dv.getInt16(48, true), Math.trunc(0.5 * 0x7fff));   // setInt16 truncates
});

test("b64 matches Buffer over chunk boundaries", () => {
  const bytes = new Uint8Array(70000);
  for (let i = 0; i < bytes.length; i++) bytes[i] = i % 251;
  assert.equal(b64(bytes), Buffer.from(bytes).toString("base64"));
});
