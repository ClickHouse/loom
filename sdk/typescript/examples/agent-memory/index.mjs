// An agent that remembers, recalls, answers with citations and closes the loop.
// Run against any Loom:  LOOM_BASE_URL=… LOOM_API_KEY=… node examples/agent-memory/index.mjs
import { Datapack, LoomError, NotFound, probe } from "@clickhouse/loom";

const org = process.env.LOOM_ORG ?? "dev";
const namespace = process.env.LOOM_NAMESPACE ?? `ts-example-${Math.random().toString(16).slice(2, 8)}`;
const pack = new Datapack(org, namespace, { consumer: "ts-example" }); // LOOM_BASE_URL / LOOM_API_KEY from env
const bot = pack.agent("travel-assistant");
const scope = { userId: "u-42" };

// 1. Does this deployment serve what the loop needs?
const p = await probe(pack.client, { ops: ["memory.set", "memory.search", "memory.ask", "memory.outcome"] });
console.log(`server ${p.buildCommit}: ok=${p.ok}, ops the SDK does not know: ${p.unknownToSdk.length}`);

// 2. A throwaway namespace for the example (an operator registers real ones). Registering
// one that already exists takes its admin, so a refusal here means "it is there already".
await pack.client
  .callRaw("namespace.register", { org, namespace, owner_email: "example@loom.test", kind: "bench" })
  .catch((e) => {
    if (!(e instanceof LoomError)) throw e;
  });

// What the user said across two conversations; step 8 deletes exactly these.
const facts = {
  seat_pref: "The user prefers an aisle seat on flights over three hours.",
  meal_pref: "The user asked for a vegetarian meal on their last two bookings.",
  airline_status: "The user holds gold status with Lufthansa (as of 2026-08).",
};

try {
  // 3. Remember three things the user said across two conversations.
  for (const [key, text] of Object.entries(facts)) await bot.remember(key, text, { ...scope, tags: ["travel"] });

  // 4. Recall by meaning.
  const hits = await bot.recall("what seat should I book for a long flight?", { ...scope, top_k: 3 });
  for (const h of hits.results) console.log(`  recall ${h.similarity?.toFixed(2)}  [${h.memory_key}] ${h.content_excerpt}`);

  // 5. Ask for a grounded answer; loom abstains when the memories do not say.
  const answer = await bot.ask("Which seat and meal should I request for the user's flight to Tokyo?", scope);
  console.log(`ask → abstain=${answer.abstain} cited=${JSON.stringify(answer.memory_keys)}`);
  console.log(`     ${answer.answer}`);

  // 6. Close the loop: the memories the agent actually used earn their keep.
  for (const key of answer.memory_keys ?? []) await bot.outcome(key, "used", scope);

  // 7. Errors are typed; a missing key reads as found=false, an unknown op is NotFound.
  const miss = await bot.call("memory.get", { memory_key: "never_written" }, scope);
  console.log(`memory.get of a missing key → found=${miss.found}`);
  try {
    await pack.client.callRaw("memory.no_such_op", {});
  } catch (e) {
    console.log(`unknown op → ${e.constructor.name} (${e.status}) request ${e.requestId.slice(0, 8)}…`);
    if (!(e instanceof NotFound)) throw e;
  }
} finally {
  // 8. Leave nothing behind: tombstone what step 3 wrote, even when a later step failed.
  // (Dropping the namespace itself takes an admin.)
  for (const key of Object.keys(facts)) await bot.call("memory.delete", { memory_key: key }, scope);
}
