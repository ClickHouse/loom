import { describe, expect, it } from "vitest";
import { namesMe, subjectLabel } from "./grantSubject";

describe("grant subjects", () => {
  it("names the person under any spelling, in any case, as the gate matches", () => {
    expect(namesMe({ subject: "Alice@Example.com", also: ["alice"] }, ["ALICE"])).toBe(true);
    expect(namesMe({ subject: "bob" }, ["alice"])).toBe(false);
    expect(namesMe({ subject: "bob" }, [""])).toBe(false);
  });

  it("reads groups, agents, service accounts, end users and everyone as the header does", () => {
    expect(subjectLabel("group:eng")).toBe("group eng");
    expect(subjectLabel("agent:helper")).toBe("agent helper");
    expect(subjectLabel("sa:reporter-a1b2c3")).toBe("Reporter bot");
    expect(subjectLabel("user:u1")).toBe("u1");
    expect(subjectLabel("everyone", "acme")).toBe("Everyone in acme");
  });
});
