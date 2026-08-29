/* One RAG instance for every namespace (= source). Stored vectors are
   pushed precomputed from the Mac (local mxbai-embed-large, 1024-dim);
   the model here is only used to embed queries, via Mixedbread's
   OpenAI-compatible endpoint — the same one deploy/lambda/app.py calls,
   whose parity with the local model tools/eval-embed-parity.py checked.
   Filters are equality-only; that constraint is what the spike measures. */
import { components } from "./_generated/api";
import { RAG } from "@convex-dev/rag";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";

export type Filters = {
  day: string;      // local YYYY-MM-DD
  month: string;    // local YYYY-MM — OR'd across a window
  locpfx: string;   // server._loc_prefix() of the chunk location
  done: string;     // "1" | "0", tasks only
};
export const FILTER_NAMES = ["day", "month", "locpfx", "done"] as const;

/* Any OpenAI-compatible host of mxbai-embed-large-v1, swappable by env:
   EMBED_BASE_URL, EMBED_API_KEY, EMBED_MODEL. Mixedbread's API goes cold
   after ~1 min idle (see crons.ts); the only other host found serving this
   model is HF Inference, whose API isn't OpenAI-shaped. Vectors must match
   the local model — run tools/eval-embed-parity.py on any new host. */
const provider = createOpenAICompatible({
  name: "embed",
  baseURL: process.env.EMBED_BASE_URL ?? "https://api.mixedbread.com/v1",
  apiKey: process.env.EMBED_API_KEY ?? process.env.MXBAI_API_KEY ?? "",
});

export const queryModel = provider.textEmbeddingModel(
  process.env.EMBED_MODEL ?? "mixedbread-ai/mxbai-embed-large-v1",
);

export const rag = new RAG<Filters>(components.rag, {
  textEmbeddingModel: queryModel,
  embeddingDimension: 1024,
  filterNames: [...FILTER_NAMES],
});

/* Matches [core] mxbai_query_prompt on the Mac (default ""), so query
   vectors land in the same space as the Lambda's. */
export const QUERY_PROMPT = process.env.MXBAI_QUERY_PROMPT ?? "";
