// Type-checks every ```ts block in README.md against src/, so an example cannot
// drift from the API it shows. Each block is its own module; the free names the
// examples lean on (baseUrl, org, bot, …) are declared, missing imports added.
import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const out = join(root, "node_modules", ".cache", "readme-snippets");

const FREE = {
  baseUrl: "string",
  apiKey: "string",
  org: "string",
  namespace: "string",
  agent: "string",
  input: "any",
  userText: "string",
  reply: "string",
  buildLog: "string",
  model: "string",
  messages: "any[]",
  openai: "any",
  schedule: "(s: number) => void",
  poll: "() => void",
  myPinoAdapter: 'import("@clickhouse/loom").Logger',
  loom: 'import("@clickhouse/loom").LoomClient',
  bot: 'import("@clickhouse/loom").Agent',
};
const EXPORTS = ["LoomClient", "Datapack", "Agent", "NotFound", "RateLimited", "probe", "collect", "paginate"];

const readme = readFileSync(join(root, "README.md"), "utf8");
const blocks = [...readme.matchAll(/```ts\n([\s\S]*?)```/g)].map((m) => m[1]);

rmSync(out, { recursive: true, force: true });
mkdirSync(out, { recursive: true });
blocks.forEach((raw, i) => {
  // `if (x) …` stands for "do something": give it a body.
  const body = raw.replace(/\) …/g, ") {}");
  const head = [];
  for (const name of EXPORTS) {
    if (new RegExp(`\\b${name}\\b`).test(body) && !new RegExp(`import[^;]*\\b${name}\\b`).test(body)) {
      head.push(`import { ${name} } from "@clickhouse/loom";`);
    }
  }
  for (const [name, type] of Object.entries(FREE)) {
    const used = new RegExp(`\\b${name}\\b`).test(body);
    const bound = new RegExp(`\\b(const|let)\\s+(\\{[^}]*\\b${name}\\b[^}]*\\}|${name}\\b)`).test(body);
    if (used && !bound) head.push(`declare const ${name}: ${type};`);
  }
  writeFileSync(join(out, `block${String(i).padStart(2, "0")}.mts`), `${head.join("\n")}\n${body}\nexport {};\n`);
});

writeFileSync(
  join(out, "tsconfig.json"),
  JSON.stringify({
    extends: join(root, "tsconfig.json"),
    compilerOptions: {
      noUnusedLocals: false,
      typeRoots: [join(root, "node_modules", "@types")],
      paths: {
        "@clickhouse/loom": [join(root, "src", "index.ts")],
        "@clickhouse/loom/integrations/langchain": [join(root, "src", "integrations", "langchain.ts")],
        "@clickhouse/loom/integrations/mastra": [join(root, "src", "integrations", "mastra.ts")],
        "@clickhouse/loom/integrations/openai-agents": [join(root, "src", "integrations", "openai-agents.ts")],
        "@clickhouse/loom/integrations/strands": [join(root, "src", "integrations", "strands.ts")],
      },
    },
    include: [join(out, "*.mts")],
  }),
);

try {
  execFileSync(join(root, "node_modules", ".bin", "tsc"), ["-p", join(out, "tsconfig.json")], { stdio: "pipe" });
} catch (e) {
  process.stderr.write(String(e.stdout));
  console.error(`README.md: a ts example does not type-check (blocks are in ${out})`);
  process.exit(1);
}
console.log(`README.md: ${blocks.length} ts examples type-check`);
