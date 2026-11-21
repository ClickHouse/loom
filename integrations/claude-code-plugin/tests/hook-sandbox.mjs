import fs from "node:fs";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

export async function removeSandbox(box, { timeoutMs = 45000 } = {}) {
  const deadline = performance.now() + timeoutMs;
  // The flusher removes its job only after its last cursor/recording write.
  while (fs.readdirSync(box.tmp).some((name) => /^loom-flush-\d+-\d+\.json$/.test(name))) {
    if (performance.now() >= deadline) {
      const jobs = fs.readdirSync(box.tmp).filter((name) => /^loom-flush-\d+-\d+\.json$/.test(name));
      const pending = jobs.slice(0, 8).map((name) => ({
        name,
        bytes: fs.statSync(path.join(box.tmp, name), { throwIfNoEntry: false })?.size ?? null,
      }));
      throw new Error(
        `unfinished detached jobs in hook sandbox: ${JSON.stringify({ count: jobs.length, jobs: pending })}`,
      );
    }
    await delay(10);
  }
  fs.rmSync(box.dir, { recursive: true, force: true });
}
