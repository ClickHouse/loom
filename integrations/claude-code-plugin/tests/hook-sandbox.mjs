import fs from "node:fs";
import { setTimeout as delay } from "node:timers/promises";

export async function removeSandbox(box, { timeoutMs = 45000 } = {}) {
  const deadline = performance.now() + timeoutMs;
  // The flusher removes its job only after its last cursor/recording write.
  while (fs.readdirSync(box.tmp).some((name) => /^loom-flush-\d+-\d+\.json$/.test(name))) {
    if (performance.now() >= deadline) throw new Error("unfinished detached jobs in hook sandbox");
    await delay(10);
  }
  fs.rmSync(box.dir, { recursive: true, force: true });
}
