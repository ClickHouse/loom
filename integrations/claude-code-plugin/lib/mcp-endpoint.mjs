// Claude Code's single endpoint also determines where its hooks call HTTP ops.
const CURATED = new Set(["core", "coding", "atomic", "tasks", "tracker"]);
const WHOLE = new Set(["readonly", "assistant", "all"]);

export class EndpointError extends Error {
  constructor() {
    super(
      'Loom: configure server_url as the full MCP URL, for example "https://<loom>/mcp?toolset=coding"; keep any proxy prefix and put the desired toolset in the URL.',
    );
    this.name = "EndpointError";
  }
}

/** A complete endpoint, its control-plane base and the explicitly requested roster.
 *  Missing/unknown toolsets use the server's setting, which the client cannot infer. */
export function parseEndpoint(value) {
  try {
    const text = String(value || "").trim();
    const url = new URL(text);
    if (!/^https?:$/.test(url.protocol) || url.username || url.password || url.hash) throw new EndpointError();
    if (!url.pathname.endsWith("/mcp") || /[\\\s]/.test(text) || /%2f|%5c/i.test(url.pathname))
      throw new EndpointError();
    const names = [...url.searchParams.keys()];
    if (new Set(names).size !== names.length) throw new EndpointError();
    const raw = url.searchParams.get("toolset");
    const parts = [
      ...new Set(
        String(raw || "")
          .toLowerCase()
          .split(/[+ ,]+/)
          .filter(Boolean),
      ),
    ];
    const known =
      parts.length === 1
        ? CURATED.has(parts[0]) || WHOLE.has(parts[0])
        : parts.length > 1 && parts.every((p) => CURATED.has(p));
    return { url: text, base: url.origin + url.pathname.slice(0, -4), toolset: known ? parts.join("+") : null };
  } catch {
    // Never echo the supplied value: query parameters and userinfo may hold secrets.
    throw new EndpointError();
  }
}

/** Defaults expand LOOM_BASE_URL in the manifest; explicit install values win. */
export function claudeEndpoint(env = process.env) {
  return parseEndpoint(
    env.CLAUDE_PLUGIN_OPTION_SERVER_URL ??
      env.CLAUDE_CODE_MCP_SERVER_URL ??
      `${env.LOOM_BASE_URL || "http://localhost:7777"}/mcp?toolset=coding`,
  );
}
