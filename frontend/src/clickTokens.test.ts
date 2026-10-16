import { describe, expect, it } from "vitest";

// src/click-tokens.css is generated from the installed Click UI package; a styles.css edit
// that reads a new Click UI variable, or a package bump, must regenerate it with
// `node scripts/click_tokens.mjs`.
describe("click-tokens.css", () => {
  it("matches the installed Click UI and the variables styles.css reads", async () => {
    // vitest runs on node; the frontend tsconfig carries no node types, so the modules
    // are reached through computed specifiers and cast to the calls this test makes.
    type Fs = { readFileSync(p: string, enc: "utf8"): string };
    type Gen = { clickTokensCss(): string };
    const fs = (await import(/* @vite-ignore */ "node:" + "fs")) as Fs;
    const gen = (await import(/* @vite-ignore */ "../scripts/" + "click_tokens.mjs")) as Gen;
    const current = fs.readFileSync("src/click-tokens.css", "utf8");
    expect(current, "src/click-tokens.css is stale: run node scripts/click_tokens.mjs").toBe(gen.clickTokensCss());
  });
});
