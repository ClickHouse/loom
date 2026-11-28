import { readFileSync, writeFileSync } from "node:fs";

const source = new URL("../node_modules/@clickhouse/click-ui/dist/esm/theme/styles/tokens-dark.css", import.meta.url);
const output = new URL("../.blume-content/.click-ui-dark.css", import.meta.url);
const selector = '[data-cui-theme="dark"]';
const tokens = readFileSync(source, "utf8");
if (!tokens.includes(selector)) throw new Error("Click UI dark tokens have no theme selector");

// Blume sets data-theme in the browser. The dark site also needs tokens before that script runs.
writeFileSync(output, tokens.replace(selector, ':root:not([data-theme="light"], [data-cui-theme="light"])'));
