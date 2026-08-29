/* One RAG instance for every namespace (= source). Stored vectors are
   pushed precomputed from the Mac (local mxbai-embed-large, 1024-dim);
   the model here is only used to embed queries, via Mixedbread's
   OpenAI-compatible endpoint — the same one deploy/lambda/app.py calls,
   whose parity with the local model tools/eval-embed-parity.py checked.
   Filters are equality-only; that constraint is what the spike measures. */
import { components } from "./_generated/api";
import { RAG } from "@convex-dev/rag";
import { createOpenAICompatible } from "@ai-sdk/openai-compatible";
import { embed } from "ai";

export type Filters = {
  day: string;      // local YYYY-MM-DD
  month: string;    // local YYYY-MM — OR'd across a window
  locpfx: string;   // server._loc_prefix() of the chunk location
  done: string;     // "1" | "0", tasks only
};
export const FILTER_NAMES = ["day", "month", "locpfx", "done"] as const;

/* Query embedding host. Mixedbread's API (the default; same call the
   Lambda makes) goes cold after ~1 min idle — see crons.ts. Set
   EMBED_PROVIDER=hf plus HF_TOKEN to use Hugging Face Inference instead,
   which serves the same model through its feature-extraction API. Vectors
   must match the local model — check parity on any new host. */
const mixedbread = createOpenAICompatible({
  name: "mixedbread",
  baseURL: "https://api.mixedbread.com/v1",
  apiKey: process.env.MXBAI_API_KEY ?? "",
});
export const queryModel = mixedbread.textEmbeddingModel(
  "mixedbread-ai/mxbai-embed-large-v1",
);

const HF_URL = "https://router.huggingface.co/hf-inference/models/"
  + "mixedbread-ai/mxbai-embed-large-v1/pipeline/feature-extraction";

async function embedHf(text: string): Promise<number[]> {
  const r = await fetch(HF_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json",
               Authorization: `Bearer ${process.env.HF_TOKEN ?? ""}` },
    body: JSON.stringify({ inputs: text }),
  });
  if (!r.ok) throw new Error(`hf ${r.status}: ${(await r.text()).slice(0, 200)}`);
  const v = await r.json();
  return Array.isArray(v[0]) ? v[0] : v;
}

export const EMBED_PROVIDER = process.env.EMBED_PROVIDER ?? "mixedbread";

export async function embedQuery(text: string): Promise<number[]> {
  if (EMBED_PROVIDER === "hf") return embedHf(text);
  return (await embed({ model: queryModel, value: text })).embedding;
}

export const rag = new RAG<Filters>(components.rag, {
  textEmbeddingModel: queryModel,
  embeddingDimension: 1024,
  filterNames: [...FILTER_NAMES],
});

/* Matches [core] mxbai_query_prompt on the Mac (default ""), so query
   vectors land in the same space as the Lambda's. */
export const QUERY_PROMPT = process.env.MXBAI_QUERY_PROMPT ?? "";
