// Imported first by every test file: a recalled-data block's id key is created under
// ~/.loom (lib/prompt-recall.mjs), so HOME points at a fresh temporary directory and
// the runner's real home is never written (USERPROFILE too: os.homedir() reads it on
// Windows). recall-key-home.test.mjs checks the import.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const home = fs.mkdtempSync(path.join(os.tmpdir(), "loom-test-home-"));
process.env.HOME = home;
process.env.USERPROFILE = home;
process.on("exit", () => fs.rmSync(home, { recursive: true, force: true }));
