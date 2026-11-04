import { describe, expect, it } from "vitest";
import { tokenReadScope } from "./tokenScope";

function jwt(payload: Record<string, unknown>): string {
  const b64 = (s: string) => btoa(s).replace(/=+$/, "").replace(/\+/g, "-").replace(/\//g, "_");
  return `${b64(JSON.stringify({ alg: "HS256", typ: "JWT" }))}.${b64(JSON.stringify(payload))}.sig`;
}

describe("tokenReadScope", () => {
  it("names a read scope that pins the token to one user or session, and nothing wider", () => {
    expect(tokenReadScope(jwt({ sub: "a", read_scope: "own_user" }))?.readScope).toBe("own_user");
    expect(tokenReadScope(jwt({ sub: "a", read_scope: "own_session" }))?.readScope).toBe("own_session");
    expect(tokenReadScope(jwt({ sub: "a", read_scope: "namespace" }))?.readScope).toBeNull();
    expect(tokenReadScope(jwt({ sub: "a" }))?.readScope).toBeNull();
  });

  it("reads a read_user_id alone as own_user, trimmed as the server reads it", () => {
    expect(tokenReadScope(jwt({ sub: "a", read_user_id: "ada" }))).toEqual({
      readScope: "own_user",
      readUserId: "ada",
    });
    expect(tokenReadScope(jwt({ sub: "a", read_user_id: "ada", read_scope: "own_session" }))?.readScope).toBe(
      "own_session",
    );
    expect(tokenReadScope(jwt({ sub: "a", read_user_id: "" }))?.readScope).toBeNull();
    expect(tokenReadScope(jwt({ sub: "a", read_user_id: " " }))?.readScope).toBeNull();
    expect(tokenReadScope(jwt({ sub: "a", read_scope: "own_user", read_user_id: " " }))?.readUserId).toBeNull();
    expect(tokenReadScope(jwt({ sub: "a", read_scope: "own_user" }))?.readUserId).toBeNull();
  });

  it("is null for a missing or unparseable token", () => {
    expect(tokenReadScope(null)).toBeNull();
    expect(tokenReadScope("not-a-jwt")).toBeNull();
  });
});
