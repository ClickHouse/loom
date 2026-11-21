import { afterEach, expect, it, vi } from "vitest";
import { ApiError } from "../../api";
import { refusalReason } from "./chatStore";
import { readAssistantCheck } from "./readiness";

afterEach(() => vi.unstubAllGlobals());

it.each([
  "model_required",
  "budget_required",
  "model_price_unknown",
  "provider_key_missing",
  "bedrock_configuration_missing",
  "bedrock_requires_direct_route",
])("keeps native configuration failure %s consistent across chat and readiness", async (reason) => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            checks: { assistant: { ok: false, reason, required: false } },
          }),
          { status: 200 },
        ),
    ),
  );
  expect(await readAssistantCheck()).toBe("failing");
  expect(
    refusalReason(
      new ApiError({
        status: 503,
        code: "ASSISTANT_UNAVAILABLE",
        message: "configuration failed",
        detail: { reason },
      }),
    ),
  ).toEqual({ kind: "failing" });
});
