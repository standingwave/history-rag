/* Voice capture, server half (wip/SPEC-voice-capture.md): transcribe a
   short WAV clip with the parse preset — vocabulary-biased toward the
   user's own task, list and timer names — then run the transcript
   through the same pipeline as typed ✨ commands. No new write
   authority: the result is a proposal; the client queues confirmed
   chips through the normal authed mutations. */
import { v } from "convex/values";
import { action, internalAction, type ActionCtx } from "./_generated/server";
import { requireUserAction } from "./auth";
import { chatOnce, parsePreset } from "./ask";
import { gather, parseText, type ParseResult } from "./command";

const MAX_B64 = 2_000_000;   // ~45 s of 16 kHz mono WAV as base64
const TIMEOUT_MS = 12_000;

const INSTR = "Transcribe the attached audio: one short spoken command in English. " +
  "Reply with only the transcript, nothing else.";

export type VoiceResult = ParseResult & { heard: string };

async function commandCore(ctx: ActionCtx, audio: string, today: string): Promise<VoiceResult> {
  const t0 = Date.now();
  {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(today)) throw new Error("bad day");
    if (!audio || audio.length > MAX_B64) throw new Error("bad audio");
    const preset = parsePreset();
    if (!preset) throw new Error("voice needs a parse model — check ASK_MODELS");
    // An empty sentence references no list, so gather returns names only —
    // exactly the vocabulary worth biasing the transcription toward.
    const pctx = await gather(ctx, today, "");
    const names = [
      pctx.tasks.length ? `tasks: ${pctx.tasks.map((t) => t.text).join("; ")}` : "",
      pctx.lists.length ? `lists: ${pctx.lists.map((l) => l.name).join(", ")}` : "",
      pctx.timers.length ? `timers: ${pctx.timers.map((t) => t.label).join(", ")}` : "",
    ].filter(Boolean).join(". ");
    const got = await Promise.race([
      chatOnce(preset,
        INSTR + (names ? ` The speaker's app knows these names, which they may say: ${names}.` : ""),
        [{ type: "text", text: "The audio:" },
         { type: "input_audio", input_audio: { data: audio, format: "wav" } }], 200),
      new Promise<never>((_, rej) =>
        setTimeout(() => rej(new Error("timeout")), TIMEOUT_MS)),
    ]);
    const heard = got.text.trim().slice(0, 500);
    if (!heard) throw new Error("didn't catch that — try again closer to the mic");
    // ms covers both passes — the client shows it as the round trip.
    return { ...(await parseText(ctx, heard, today)), heard, ms: Date.now() - t0 };
  }
}

const cmdArgs = { audio: v.string(), today: v.string() };
export const command = action({
  args: cmdArgs,
  handler: async (ctx, { audio, today }): Promise<VoiceResult> => {
    await requireUserAction(ctx);
    return commandCore(ctx, audio, today);
  },
});
export const commandInternal = internalAction({
  args: cmdArgs,
  handler: (ctx, { audio, today }): Promise<VoiceResult> => commandCore(ctx, audio, today),
});
