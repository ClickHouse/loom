import { cpSync, existsSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { build } from "esbuild";

const source = "src/integrations/n8n";
const destination = "dist/integrations/n8n";
const entries = [
  "credentials/LoomApi.credentials",
  "nodes/Loom/Loom.node",
  "nodes/LoomMemory/LoomMemory.node",
  "nodes/LoomMemory/LoomChatHistory",
  "nodes/client",
];
const result = await build({
  entryPoints: Object.fromEntries(entries.map((entry) => [entry, `${source}/${entry}.ts`])),
  outdir: destination,
  bundle: true,
  platform: "node",
  format: "cjs",
  define: { "import.meta.url": "__filename" },
  target: "node20",
  external: ["n8n-workflow"],
  metafile: true,
  legalComments: "eof",
});

// The custom-node loader requires .js files; the SDK's root package is ESM.
writeFileSync(join(destination, "package.json"), JSON.stringify({ type: "commonjs" }));
for (const file of ["credentials/loom.svg", "nodes/Loom/loom.svg", "nodes/LoomMemory/loom.svg", "LICENSE"]) {
  cpSync(join(source, file), join(destination, file));
}

// Ship the licenses of bundled libraries alongside the nodes.
const licenses = new Map();
// This registry tarball omits its license; the copy is from its pinned source commit.
const sourceLicenses = { "js-tiktoken@1.0.21": `${source}/vendor-licenses/js-tiktoken.txt` };
const used = new Set(
  Object.values(result.metafile.outputs).flatMap((output) =>
    Object.entries(output.inputs)
      .filter(([, contribution]) => contribution.bytesInOutput > 0)
      .map(([input]) => input),
  ),
);
for (const input of used) {
  if (!input.includes("node_modules/")) continue;
  let directory = dirname(resolve(input));
  let metadata;
  for (;;) {
    if (existsSync(join(directory, "package.json"))) {
      metadata = JSON.parse(readFileSync(join(directory, "package.json"), "utf8"));
      if (metadata.name) break;
    }
    const parent = dirname(directory);
    if (parent === directory) throw new Error(`No package metadata for ${input}`);
    directory = parent;
  }
  if (licenses.has(metadata.name)) continue;
  const files = readdirSync(directory).filter((name) => /^(license|copying|notice)(\.|$)/i.test(name));
  const sourceLicense = sourceLicenses[`${metadata.name}@${metadata.version}`];
  if (files.length === 0 && !sourceLicense) throw new Error(`No license file for bundled ${metadata.name}`);
  licenses.set(
    metadata.name,
    `${metadata.name} ${metadata.version}\n${[...files.map((name) => readFileSync(join(directory, name), "utf8")), ...(sourceLicense ? [readFileSync(sourceLicense, "utf8")] : [])].join("\n")}`,
  );
}
writeFileSync(join(destination, "THIRD_PARTY_LICENSES.txt"), [...licenses.values()].join("\n\n---\n\n"));
