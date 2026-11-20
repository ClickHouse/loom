import { OP_MIN_ROLE } from "../lib/canCall";

// Every op the registry serves: the SDK's generated table
// (`sdk/typescript/src/generated/ops.ts`, written by `scripts/codegen/sdk_ts_gen.py` from
// `server.operations.registry`), together with the ops the dashboard gates on
// (`OP_MIN_ROLE`), which is all a frontend-only checkout has.

// Vite serves a file outside the project's root under `/@fs/`, URL-encoded, which is not a
// path on disk.
const onDisk = (rel: string) => decodeURIComponent(new URL(rel, import.meta.url).pathname).replace(/^\/@fs\//, "/");
const SDK_TREE = onDisk("../../../sdk/typescript");
const GENERATED_OPS = onDisk("../../../sdk/typescript/src/generated/ops.ts");

/** The generated op table's source, or null in a frontend-only checkout. A checkout with the
 *  SDK tree must have the table: a partial list would pass every check that reads it. */
export async function generatedOpsText(): Promise<string | null> {
  // vitest runs on node; the frontend tsconfig carries no node types, so the module is
  // reached through a computed specifier, and the cast names the two calls used here.
  type Fs = { existsSync(p: string): boolean; readFileSync(p: string, enc: "utf8"): string };
  const fs = (await import(/* @vite-ignore */ "node:" + "fs")) as Fs;
  if (fs.existsSync(GENERATED_OPS)) return fs.readFileSync(GENERATED_OPS, "utf8");
  if (fs.existsSync(SDK_TREE)) throw new Error(`sdk/typescript is here but ${GENERATED_OPS} is not`);
  return null;
}

export async function opNames(): Promise<{ names: Set<string>; fromRegistry: boolean }> {
  const names = new Set(Object.keys(OP_MIN_ROLE).filter((op) => op.includes(".")));
  const text = await generatedOpsText();
  if (text === null) return { names, fromRegistry: false };
  for (const m of text.matchAll(/\{ name: "([a-z0-9_.]+)",/g)) names.add(m[1]);
  return { names, fromRegistry: true };
}

export const escapeRegExp = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
