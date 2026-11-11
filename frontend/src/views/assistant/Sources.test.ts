import { describe, expect, it } from "vitest";
import type { Citation } from "../../api/assistantChat";
import { supportLabel } from "./Sources";

let n = 0;
const cite = (verified: boolean, kind = "memory"): Citation => ({
  kind,
  text: `${kind}:k${n++}`,
  verified,
  reason: verified ? "" : "not found",
  ref: "k",
});

describe("supportLabel", () => {
  it("counts one source in the singular", () => {
    expect(supportLabel("unsupported", [cite(false)])?.text).toBe("Couldn't confirm: 0 of 1 source found in Loom");
  });

  it("says what was found, not that every source was compared, when some were not checked", () => {
    const label = supportLabel("supported", [cite(true), cite(true), cite(false, "code")]);
    expect(label?.text).toBe("Partly confirmed: 2 of 3 sources found in Loom");
    expect(label?.why).toBe(
      "Loom found 2 of the 3 sources this answer cites in what Loom holds or in what the answer's own reads returned; each other source says why.",
    );
  });

  it("says none was found on an unsupported answer with sources", () => {
    expect(supportLabel("unsupported", [cite(false), cite(false)])?.why).toBe(
      "Loom found none of the sources this answer cites in what Loom holds or in what the answer's own reads returned; each source below says why.",
    );
  });

  it("says each was found when all were", () => {
    expect(supportLabel("supported", [cite(true)])?.why).toBe(
      "Loom found each source this answer cites in what Loom holds or in what the answer's own reads returned.",
    );
  });
});
