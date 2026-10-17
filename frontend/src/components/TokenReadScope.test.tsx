import { cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TokenReadScopePanel } from "./TokenReadScope";

afterEach(cleanup);

describe("TokenReadScopePanel", () => {
  it("names the home namespace as the write target and lists every readable namespace and repository", () => {
    render(
      <TokenReadScopePanel
        scope={{
          home: "me",
          namespaces: ["me", "team"],
          repositories: [
            {
              repo: "org/ch",
              branches: [
                { namespace: "code-ch", branch: "master" },
                { namespace: "code-ch-26-8", branch: "26.8" },
              ],
            },
          ],
          excludedRestricted: [],
          pending: [],
        }}
      />,
    );
    expect(screen.getByText("Writes to")).toBeTruthy();
    expect(screen.getByText("Reads 2 namespaces", { exact: false })).toBeTruthy();
    expect(screen.getByText("and 1 repository", { exact: false })).toBeTruthy();
    expect(screen.getByText("team")).toBeTruthy();
    expect(screen.getByText("org/ch", { exact: false })).toBeTruthy();
    expect(screen.queryByTestId("token-read-scope-restricted")).toBeNull();
  });

  it("a restricted repository is called out as not included, with the reason", () => {
    render(
      <TokenReadScopePanel
        scope={{
          home: "me",
          namespaces: ["me"],
          repositories: [],
          excludedRestricted: [{ repo: "org/private", branches: [{ namespace: "code-private", branch: "master" }] }],
          pending: ["shared"],
        }}
        docsHref="https://example.test/docs"
      />,
    );
    const restricted = screen.getByTestId("token-read-scope-restricted");
    expect(restricted.textContent).toContain("Not included");
    expect(restricted.textContent).toContain("org/private");
    expect(restricted.textContent).toContain("restricted repository");
    expect(screen.getByRole("link", { name: "How to get one (opens in a new tab)" }).getAttribute("href")).toBe(
      "https://example.test/docs",
    );
    expect(screen.getByText("Awaiting approval: shared", { exact: false })).toBeTruthy();
  });

  it("with a toggle, a restricted repository is a checkbox; ticked, it moves into the read set with a lock", () => {
    const onToggle = vi.fn();
    const scope = {
      home: "me",
      namespaces: ["me"],
      repositories: [],
      excludedRestricted: [{ repo: "org/private", branches: [{ namespace: "code-private", branch: "master" }] }],
      pending: [],
    };
    const { rerender } = render(<TokenReadScopePanel scope={scope} included={new Set()} onToggleRepo={onToggle} />);
    const box = screen.getByRole("checkbox", { name: "Add org/private to this token" });
    expect(box).not.toBeChecked();
    expect(screen.queryByTestId("token-read-scope-included")).toBeNull();
    fireEvent.click(box);
    expect(onToggle).toHaveBeenCalledWith(scope.excludedRestricted[0]);
    rerender(<TokenReadScopePanel scope={scope} included={new Set(["org/private"])} onToggleRepo={onToggle} />);
    expect(screen.getByRole("checkbox", { name: "Add org/private to this token" })).toBeChecked();
    expect(screen.getByTestId("token-read-scope-included").textContent).toContain("org/private");
  });
});
