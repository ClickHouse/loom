// Every test file gets its own empty home: a client given no key looks for a
// stored login and `~/.loom/token` there, never in the developer's own.
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const home = mkdtempSync(join(tmpdir(), "loom-sdk-home-"));
process.env.HOME = home;
process.env.USERPROFILE = home;
delete process.env.LOOM_PROFILE;
