#!/usr/bin/env node
// Exchange the agent's workload token for an hour-long Loom token (RFC 8693)
// and save it, 0600, where resolveToken reads it (lib/loom-client.mjs, agent
// mode). resolveToken runs this when the saved token is about to run out.
// Exits 1 when the exchange did not happen; writes nothing else.
import { readFileSync } from "node:fs";

import { agentTokenPath, resolveServerUrl, writePrivateFile } from "../lib/loom-client.mjs";

const TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange";

export async function exchange({ env = process.env, fetchImpl = fetch, now = Date.now() / 1000 } = {}) {
  const workload = (env.LOOM_WORKLOAD_TOKEN || readFileSync(env.LOOM_WORKLOAD_TOKEN_FILE, "utf8")).trim();
  const server = resolveServerUrl();
  const r = await fetchImpl(`${server}/oauth/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: TOKEN_EXCHANGE,
      client_id: env.LOOM_CLIENT_ID,
      subject_token: workload,
      subject_token_type: "urn:ietf:params:oauth:token-type:jwt",
    }),
    signal: AbortSignal.timeout(2500),
  });
  const body = r.ok ? await r.json() : {};
  if (!body.access_token) return false;
  const ttl = Number(body.expires_in || 0);
  const saved = { server, client_id: env.LOOM_CLIENT_ID, token: body.access_token, exp: now + ttl, ttl };
  writePrivateFile(agentTokenPath(env), JSON.stringify(saved));
  return true;
}

if (process.env.LOOM_HOOK_NO_MAIN !== "1") {
  exchange().then(
    (ok) => process.exit(ok ? 0 : 1),
    () => process.exit(1),
  );
}
