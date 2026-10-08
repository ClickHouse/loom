/**
 * What runtime the SDK is running in, for the `X-Loom-Runtime` header
 * `node/22.23.2 linux x64`, `bun/1.2.0 darwin arm64`,
 * `deno/2.1.0 …`, `workers`, `browser`. Nothing that identifies the host.
 */

type ProcessLike = {
  versions?: Record<string, string | undefined>;
  platform?: string;
  arch?: string;
  argv?: string[];
  env?: Record<string, string | undefined>;
};

function proc(): ProcessLike | undefined {
  return (globalThis as { process?: ProcessLike }).process;
}

/**
 * One environment variable, or undefined where there is no `process` or the
 * runtime refuses the read (Deno without `--allow-env` throws).
 */
export function envVar(name: string): string | undefined {
  try {
    return proc()?.env?.[name];
  } catch {
    return undefined;
  }
}

/** The running script's file name without its extension, where the runtime has an argv. */
export function scriptName(): string | undefined {
  try {
    const base = String(proc()?.argv?.[1] ?? "")
      .split(/[\\/]/)
      .pop();
    return base ? base.replace(/\.(m?js|cjs|ts)$/, "") : undefined;
  } catch {
    return undefined;
  }
}

export function isBrowser(): boolean {
  const g = globalThis as { window?: unknown; document?: unknown };
  return typeof g.window !== "undefined" && typeof g.document !== "undefined";
}

export function runtimeIdent(): string {
  const g = globalThis as { Bun?: { version?: string }; Deno?: { version?: { deno?: string } }; EdgeRuntime?: unknown };
  const p = proc();
  const platform = [p?.platform, p?.arch].filter(Boolean).join(" ");
  if (g.Bun?.version) return `bun/${g.Bun.version} ${platform}`.trim();
  if (g.Deno?.version?.deno) return `deno/${g.Deno.version.deno} ${platform}`.trim();
  if (typeof g.EdgeRuntime === "string") return "edge-runtime";
  if (p?.versions?.node) return `node/${p.versions.node} ${platform}`.trim();
  if (isBrowser()) return "browser";
  if (typeof (globalThis as { WebSocketPair?: unknown }).WebSocketPair !== "undefined") return "workers";
  return "unknown";
}
