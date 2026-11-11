import { cleanup, render, screen, within } from "../../test/rtl";
import { afterEach, describe, expect, it } from "vitest";
import type { Citation } from "../../api/assistantChat";
import { Sources, supportLabel } from "./Sources";

const cite = (verified: boolean, kind = "memory"): Citation => ({
  kind,
  text: `${kind}:k`,
  verified,
  // The server's own reasons (server/assistant/app/citations.py).
  reason: verified
    ? ""
    : kind === "code"
      ? "code citations are not checked"
      : "no memory.search, memory.ask, memory.get, memory.list, memory.inspect call in this answer was returned this key",
  ref: "k",
});

afterEach(cleanup);

describe("supportLabel", () => {
  it("counts one source in the singular", () => {
    expect(supportLabel("unsupported", [cite(false)])?.text).toBe("Couldn't confirm: 0 of 1 source found in Loom");
  });

  it("says what was found, not that every source was compared, when some were not checked", () => {
    const label = supportLabel("supported", [cite(true), cite(true), cite(false, "code")]);
    expect(label?.text).toBe("Partly confirmed: 2 of 3 sources found in Loom");
    expect(label?.why).toBe(
      "Loom found 2 of the 3 sources this answer cites in what Loom holds, in this build's documentation or in what the answer's own reads returned; each other source says why.",
    );
  });

  it("says none was found on an unsupported answer with sources", () => {
    expect(supportLabel("unsupported", [cite(false), cite(false)])?.why).toBe(
      "Loom found none of the sources this answer cites in what Loom holds, in this build's documentation or in what the answer's own reads returned; each source below says why.",
    );
  });

  it("says each was found when all were", () => {
    expect(supportLabel("supported", [cite(true)])?.why).toBe(
      "Loom found each source this answer cites in what Loom holds, in this build's documentation or in what the answer's own reads returned.",
    );
  });

  it("says the sources could not be checked on an unchecked answer with sources", () => {
    expect(supportLabel("unchecked", [cite(false, "code")])).toMatchObject({
      text: "Sources not checked",
      why: "Loom could not check this answer's sources; each source below says why.",
    });
  });
});

describe("supportLabel without citations", () => {
  it("names each cause of an uncited unchecked answer, a failed read among them", () => {
    expect(supportLabel("unchecked", [])).toMatchObject({
      text: "Not checked: it cites nothing Loom holds",
      why: "This answer cites nothing: what it read has no rows an answer can cite, a read failed or was refused, or its reads could not be checked.",
    });
  });
});

describe("Sources", () => {
  it("shows a found SQL citation as found, as text, with no reason", () => {
    const sql: Citation = { kind: "sql", text: "sql:SELECT 1", verified: true, reason: "", ref: "" };
    render(<Sources citations={[sql]} support="supported" answer={1} />);
    const row = within(screen.getByRole("list", { name: "Sources" })).getByRole("listitem");
    expect(row).toHaveTextContent(/^Found in Loom · /);
    expect(within(row).queryByRole("button")).toBeNull();
    expect(row).not.toHaveTextContent("(");
  });
});
