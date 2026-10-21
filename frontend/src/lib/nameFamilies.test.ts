import { describe, expect, it } from "vitest";
import { nameFamilies } from "./nameFamilies";

// The namespace picker folds memory namespaces whose names share a token prefix, a uuid one
// token, three or more to a group, each name in the longest prefix it shares.

describe("nameFamilies: the namespaces the picker folds into a group, and under which prefix", () => {
  const OWNER_1 = "1b4e28ba-2fa1-41d2-883f-0016d3cca427";
  const OWNER_2 = "6f1c2b7e-0d4a-4c1b-9e8f-1a2b3c4d5e6f";
  const id = (n: number) => `00000000-0000-4000-8000-00000000000${n}`;

  it("folds three or more names that share a prefix, and never two", () => {
    expect(Object.fromEntries(nameFamilies(["team-a", "team-b", "team-c", "ops-a", "ops-b", "solo"]))).toEqual({
      "team-a": "team-",
      "team-b": "team-",
      "team-c": "team-",
    });
  });

  it("reads a uuid as one part, so names that carry an owner's uuid fold per owner", () => {
    const first = [1, 2, 3].map((n) => `agent-${OWNER_1}-${id(n)}`);
    const second = [4, 5, 6].map((n) => `agent-${OWNER_2}-${id(n)}`);
    const families = nameFamilies([...first, ...second]);
    for (const name of first) expect(families.get(name)).toBe(`agent-${OWNER_1}-`);
    for (const name of second) expect(families.get(name)).toBe(`agent-${OWNER_2}-`);
  });

  it("takes the longest prefix three share, and a prefix its names left with fewer folds none", () => {
    const first = [1, 2, 3].map((n) => `agent-${OWNER_1}-${id(n)}`);
    const second = [4, 5].map((n) => `agent-${OWNER_2}-${id(n)}`);
    const families = nameFamilies([...first, ...second, "group-a-shared", "group-b-shared", "group-c-shared"]);
    for (const name of first) expect(families.get(name)).toBe(`agent-${OWNER_1}-`);
    // `agent-` is shared by five, but holds only these two once the first three take the longer prefix.
    for (const name of second) expect(families.has(name)).toBe(false);
    expect(families.get("group-b-shared")).toBe("group-");
  });

  it("folds no name without a hyphen, and no name into a prefix that is all of it", () => {
    expect(nameFamilies(["alpha", "beta", "gamma"]).size).toBe(0);
    const families = nameFamilies(["team", "team-", "team-a", "team-b", "team-c"]);
    expect(families.has("team")).toBe(false);
    expect(families.has("team-")).toBe(false);
    expect(families.get("team-a")).toBe("team-");
  });

  it("titles a family as its names are written: doubled hyphens stay doubled", () => {
    const families = nameFamilies(["a--b--c1", "a--b--c2", "a--b--c3"]);
    expect(families.get("a--b--c2")).toBe("a--b--");
  });
});
