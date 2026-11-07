// Imported first by every test file that builds a recalled-data block in its own
// process: a block's id key is created under $HOME/.loom (lib/prompt-recall.mjs), so
// HOME points at a fresh temporary directory and the runner's real home is never
// written. recall-key-home.test.mjs checks the files that need it import it.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const home = fs.mkdtempSync(path.join(os.tmpdir(), "loom-test-home-"));
process.env.HOME = home;
process.on("exit", () => fs.rmSync(home, { recursive: true, force: true }));
