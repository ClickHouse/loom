// Resume a conversation after a restart, with the session in Loom.
//
// Run 1 tells the agent a fact and exits, taking the process (and all in-memory
// state) with it. Run 2 constructs a brand-new Agent with the same session id,
// resumes it — the context pack becomes the system prompt, the transcript tail
// comes back — and asks about the fact, letting the model call Loom's tools.
//
//   LOOM_BASE_URL=… LOOM_API_KEY=… LOOM_ORG=dev LOOM_NAMESPACE=demo node index.mjs tell "My rental car is a blue Nissan Micra"
//   LOOM_BASE_URL=… LOOM_API_KEY=… LOOM_ORG=dev LOOM_NAMESPACE=demo node index.mjs ask  "What car am I driving?"
//
// With OPENAI_API_KEY set, `ask` runs a real tool-use turn against gpt-4o-mini;
// without it, the script calls the tools itself so the flow still shows.
import { Datapack } from "@clickhouse/loom";

const [verb, ...rest] = process.argv.slice(2);
const text = rest.join(" ");
if (!["tell", "ask"].includes(verb) || !text) {
  console.error('usage: node index.mjs tell "<fact>" | ask "<question>"');
  process.exit(2);
}
const org = process.env.LOOM_ORG ?? "dev";
const namespace = process.env.LOOM_NAMESPACE ?? "ts-session-resume";
const pack = new Datapack(org, namespace, { consumer: "ts-example" }); // LOOM_BASE_URL / LOOM_API_KEY from env
await pack.client.callRaw("namespace.register", { org, namespace, owner_email: "example@loom.test", kind: "bench" }).catch(() => {});

const bot = pack.agent("assistant");
const SESSION_ID = process.env.SESSION_ID ?? "session-resume-demo";
const session = bot.session(SESSION_ID, { userId: "traveller-1", flushEvery: 0 });

if (verb === "tell") {
  // A turn pair goes to the transcript now; close() sends both to memory.set_from_messages,
  // where the server extracts the facts worth keeping under this (user, session).
  await session.append(
    { role: "user", content: text },
    { role: "assistant", content: "Noted — I'll remember that for later." },
  );
  await session.note(`the traveller told us: ${text}`);
  // A large tool result is parked once; the one-line pointer goes where the next turn's pack will carry it.
  const rental = "line 1: pickup Hertz FRA T1 08:00\nline 2: plate M-AB 1234\nline 3: FAILED payment retry, card declined once\n".repeat(40);
  const blob = await session.offload(rental, { summary: "rental desk printout", blobKey: `rental/${SESSION_ID}` });
  await session.note(blob.pointer);
  console.log(`offloaded ${blob.content_bytes} bytes → ${blob.pointer.split("]")[0]}]`);
  if (session.transcriptStored === false) console.log("(conversation logging is off for this namespace: turns go to extraction only)");
  const out = await session.close();
  console.log(`flushed: extracted ${out?.written?.length ?? 0} memories:`, (out?.written ?? []).map((w) => w.memory_key));
  process.exit(0);
}

// ---- run 2: a new process, a new Agent, the same session id -----------------
const resumed = await session.resume({ query: text, history: 10 });
console.log(`resumed: ${resumed.pack.section_count} pack sections, ${resumed.messages.length} transcript turns`);
console.log(`transcript tail: ${resumed.messages.map((m) => `${m.role}: ${m.content}`).join(" | ")}`);
console.log(`system prompt (${resumed.system.length} chars) starts: ${resumed.system.slice(0, 160).replace(/\n/g, " ⏎ ")}`);

const tools = bot.tools(session.scope(), { only: ["loom_recall", "loom_ask", "loom_fetch"] });
const messages = [
  {
    role: "system",
    content: `${resumed.system}\n\nUse loom_ask to check what the traveller told you before answering. A line starting "[offloaded" is a parked document: read it with loom_fetch (blob_key from the pointer, a pattern for what you look for) before saying something is not recorded.`,
  },
  ...resumed.messages.map((m) => ({ role: m.role, content: m.content })),
  { role: "user", content: text },
];

if (process.env.OPENAI_API_KEY) {
  // A real tool-use turn: the model decides which Loom tool to call; we run it and hand the result back.
  const model = process.env.OPENAI_MODEL ?? "gpt-4o-mini";
  for (let step = 0; step < 4; step++) {
    const res = await fetch("https://api.openai.com/v1/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${process.env.OPENAI_API_KEY}` },
      body: JSON.stringify({ model, messages, tools: tools.definitions("openai"), tool_choice: "auto", temperature: 0 }),
    });
    const data = await res.json();
    const msg = data.choices?.[0]?.message;
    if (!msg) throw new Error(`openai: ${JSON.stringify(data).slice(0, 300)}`);
    messages.push(msg);
    if (!msg.tool_calls?.length) {
      console.log(`${model} → ${msg.content}`);
      break;
    }
    for (const call of msg.tool_calls) {
      const result = await tools.execute(call.function.name, call.function.arguments);
      console.log(`${model} called ${call.function.name}(${call.function.arguments}) → ${result.slice(0, 200)}`);
      messages.push({ role: "tool", tool_call_id: call.id, content: result });
    }
  }
} else {
  console.log(`(no OPENAI_API_KEY) loom_ask → ${await tools.execute("loom_ask", { query: text })}`);
}
console.log(`loom_fetch(pattern=FAILED) → ${(await tools.execute("loom_fetch", { blob_key: `rental/${SESSION_ID}`, pattern: "FAILED", limit: 200 })).slice(0, 200)}`);
process.exit(0);
