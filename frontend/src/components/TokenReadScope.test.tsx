import { cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TokenReadScopePanel } from "./TokenReadScope";

afterEach(cleanup);

const noop = () => {};

describe("TokenReadScopePanel", () => {
  it("lists every readable namespace and repository", () => {
    render(
      <TokenReadScopePanel
        scope={{
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
        included={new Set()}
        onToggleRepo={noop}
      />,
    );
    expect(screen.getByText("Reads 2 namespaces", { exact: false })).toBeTruthy();
    expect(screen.getByText("and 1 repository", { exact: false })).toBeTruthy();
    expect(screen.getByText("team")).toBeTruthy();
    expect(screen.getByText("org/ch", { exact: false })).toBeTruthy();
    expect(screen.queryByTestId("token-read-scope-restricted")).toBeNull();
  });

  it("says pending joins become readable without a new token", () => {
    render(
      <TokenReadScopePanel
        scope={{ namespaces: ["me"], repositories: [], excludedRestricted: [], pending: ["shared"] }}
        included={new Set()}
        onToggleRepo={noop}
      />,
    );
    expect(screen.getByText("Awaiting approval: shared", { exact: false })).toBeTruthy();
  });

  it("a restricted repository is a checkbox; ticked, it moves into the read set with a lock", () => {
    const onToggle = vi.fn();
    const scope = {
      namespaces: ["me"],
      repositories: [],
      excludedRestricted: [{ repo: "org/private", branches: [{ namespace: "code-private", branch: "master" }] }],
      pending: [],
    };
    const { rerender } = render(
      <TokenReadScopePanel
        scope={scope}
        included={new Set()}
        onToggleRepo={onToggle}
        docsHref="https://example.test/docs"
      />,
    );
    expect(screen.getByRole("link", { name: "Why (opens in a new tab)" }).getAttribute("href")).toBe(
      "https://example.test/docs",
    );
    const box = screen.getByRole("checkbox", { name: "Also read org/private" });
    expect(box).not.toBeChecked();
    expect(screen.queryByTestId("token-read-scope-included")).toBeNull();
    fireEvent.click(box);
    expect(onToggle).toHaveBeenCalledWith(scope.excludedRestricted[0]);
    rerender(<TokenReadScopePanel scope={scope} included={new Set(["org/private"])} onToggleRepo={onToggle} />);
    expect(screen.getByRole("checkbox", { name: "Also read org/private" })).toBeChecked();
    expect(screen.getByTestId("token-read-scope-included").textContent).toContain("org/private");
  });
});
