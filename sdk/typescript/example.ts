/**
 * Example: instrument a GLM agent pipeline with Kayba tracing (TypeScript).
 *
 * Mirrors the Python example in examples/tracing_glm_example.py.
 *
 * Run: npx tsx example.ts
 */

import "dotenv/config";
import OpenAI from "openai";
import kayba, { SpanType } from "./src/index";

// ── Kayba tracing setup ────────────────────────────────────────────────
kayba.configure({
  apiKey: process.env.KAYBA_SDK_KEY,
  baseUrl: process.env.KAYBA_BASE_URL,
  folder: "ts-sdk-examples",
});

// ── OpenAI client (GLM-compatible endpoint) ────────────────────────────
const client = new OpenAI({
  baseUrl: process.env.OPENAI_BASE_URL,
  apiKey: process.env.OPENAI_API_KEY,
});
const MODEL = "glm-5.1";

// ── Traced helper functions ────────────────────────────────────────────

const llmCall = kayba.trace(
  async (messages: OpenAI.ChatCompletionMessageParam[]) => {
    const response = await client.chat.completions.create({
      model: MODEL,
      messages,
      temperature: 0.7,
    });
    return response.choices[0].message.content ?? "";
  },
  { name: "llm_call", spanType: SpanType.LLM },
);

const researchAgent = kayba.trace(
  async (topic: string) => {
    const span = kayba.startSpan({
      name: "build_prompt",
      spanType: SpanType.TOOL,
      inputs: { topic },
    });

    const messages: OpenAI.ChatCompletionMessageParam[] = [
      {
        role: "system",
        content: "You are a research assistant. List 3 key facts.",
      },
      { role: "user", content: `Research this topic: ${topic}` },
    ];

    span.end({ outputs: { message_count: messages.length }, status: "OK" });

    return await llmCall(messages);
  },
  { name: "research_agent", spanType: SpanType.AGENT },
);

const summariserAgent = kayba.trace(
  async (facts: string) => {
    const span = kayba.startSpan({
      name: "build_prompt",
      spanType: SpanType.TOOL,
      inputs: { facts_length: facts.length },
    });

    const messages: OpenAI.ChatCompletionMessageParam[] = [
      {
        role: "system",
        content:
          "You are a summariser. Condense the following facts into one concise paragraph.",
      },
      { role: "user", content: facts },
    ];

    span.end({ outputs: { message_count: messages.length }, status: "OK" });

    return await llmCall(messages);
  },
  { name: "summariser_agent", spanType: SpanType.AGENT },
);

const runPipeline = kayba.trace(
  async (topic: string) => {
    const facts = await researchAgent(topic);
    console.log(`\n--- Research Agent ---\n${facts}`);

    const summary = await summariserAgent(facts);
    console.log(`\n--- Summariser Agent ---\n${summary}`);

    return summary;
  },
  { name: "pipeline", spanType: SpanType.CHAIN },
);

// ── Main ───────────────────────────────────────────────────────────────

async function main() {
  console.log("Running TypeScript tracing example...\n");

  const result = await runPipeline("The history of the Silk Road");
  console.log(`\n--- Final result ---\n${result}`);

  // Give MLflow time to flush traces to Kayba
  console.log("\nFlushing traces...");
  await new Promise((r) => setTimeout(r, 3000));
  console.log("Done!");
}

main().catch(console.error);
