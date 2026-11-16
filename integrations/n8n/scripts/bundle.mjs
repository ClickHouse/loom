import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { build } from "esbuild";

const result = await build({
  entryPoints: {
    "credentials/LoomApi.credentials": "credentials/LoomApi.credentials.ts",
    "nodes/Loom/Loom.node": "nodes/Loom/Loom.node.ts",
    "nodes/LoomMemory/LoomMemory.node": "nodes/LoomMemory/LoomMemory.node.ts",
    "nodes/LoomMemory/LoomChatHistory": "nodes/LoomMemory/LoomChatHistory.ts",
    "nodes/client": "nodes/client.ts",
  },
  outdir: "dist",
  bundle: true,
  platform: "node",
  format: "cjs",
  target: "node24",
  external: ["n8n-workflow"],
  metafile: true,
  legalComments: "eof",
});

// Ship the licenses of bundled libraries alongside the nodes.
const licenses = new Map();
// This registry tarball omits its license; the copy is from its pinned source commit.
const sourceLicenses = { "js-tiktoken@1.0.21": "vendor-licenses/js-tiktoken.txt" };
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
  const files = readdirSync(directory).filter((name) =>
    /^(license|copying|notice)(\.|$)/i.test(name),
  );
  const sourceLicense = sourceLicenses[`${metadata.name}@${metadata.version}`];
  if (files.length === 0 && !sourceLicense)
    throw new Error(`No license file for bundled ${metadata.name}`);
  licenses.set(
    metadata.name,
    `${metadata.name} ${metadata.version}\n${[...files.map((name) => readFileSync(join(directory, name), "utf8")), ...(sourceLicense ? [readFileSync(sourceLicense, "utf8")] : [])].join("\n")}`,
  );
}
writeFileSync("dist/THIRD_PARTY_LICENSES.txt", [...licenses.values()].join("\n\n---\n\n"));
