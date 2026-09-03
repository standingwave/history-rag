/* Stage-1 viability drill for voice capture (wip/SPEC-voice-capture.md):
   does OpenRouter carry input_audio to the parse preset, what does it
   transcribe, and how fast? One base64 WAV clip per run:

     npx convex run voiceBench:transcribe \
       "$(python3 -c 'import base64,json,sys;print(json.dumps({"audio":base64.b64encode(open(sys.argv[1],"rb").read()).decode()}))' clip.wav)"
*/
import { v } from "convex/values";
import { internalAction } from "./_generated/server";
import { chatOnce, parsePreset } from "./ask";

const INSTR = "Transcribe the attached audio: one short spoken command in English. " +
  "Reply with only the transcript, nothing else.";

export const transcribe = internalAction({
  args: { audio: v.string(), format: v.optional(v.string()), hint: v.optional(v.string()) },
  handler: async (_ctx, { audio, format = "wav", hint }) => {
    const p = parsePreset();
    if (!p) return { error: "no parse preset — check ASK_MODELS" };
    const t0 = Date.now();
    try {
      const got = await chatOnce(p, INSTR +
        (hint ? ` The speaker's app knows these names, which they may say: ${hint}` : ""), [
        { type: "text", text: "The audio:" },
        { type: "input_audio", input_audio: { data: audio, format } },
      ], 200);
      return { transcript: got.text.trim(), ms: Date.now() - t0, usage: got.usage, model: p.name };
    } catch (e) {
      return { error: String(e), ms: Date.now() - t0, model: p.name };
    }
  },
});
