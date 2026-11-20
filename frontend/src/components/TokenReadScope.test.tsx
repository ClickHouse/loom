import { cleanup, fireEvent, render, screen } from "../test/rtl";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TokenReadScopePanel } from "./TokenReadScope";

afterEach(cleanup);

const noop = () => {};

describe("TokenReadScopePanel", () => {
  it("lists every readable namespace and repository", () => {
    render(
      <TokenReadScopePanel
        readsCollections
        scope={{
          namespaces: ["me", "team"],
          collectionsOffered: false,
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

  it("reads no collection, and says once how a token reads one", () => {
    render(
      <TokenReadScopePanel
        readsCollections
        scope={{ namespaces: ["me"], collectionsOffered: true, repositories: [], excludedRestricted: [], pending: [] }}
        included={new Set()}
        onToggleRepo={noop}
      />,
    );
    const panel = screen.getByTestId("token-read-scope");
    expect(panel).toHaveTextContent("Reads 1 namespace today");
    expect(panel).not.toHaveTextContent(/1 collection|col-/);
    expect(screen.getByTestId("token-read-scope-collections")).toHaveTextContent(
      "This token reads no collection. To include a shared collection, pick “Only what you choose” and tick it.",
    );
  });

  it("says pending joins become readable without a new token", () => {
    render(
      <TokenReadScopePanel
        readsCollections
        scope={{
          namespaces: ["me"],
          collectionsOffered: false,
          repositories: [],
          excludedRestricted: [],
          pending: ["shared"],
        }}
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
      collectionsOffered: false,
      repositories: [],
      excludedRestricted: [{ repo: "org/private", branches: [{ namespace: "code-private", branch: "master" }] }],
      pending: [],
    };
    const { rerender } = render(
      <TokenReadScopePanel
        readsCollections
        scope={scope}
        included={new Set()}
        onToggleRepo={onToggle}
        docsHref="https://example.test/docs"
      />,
    );
    expect(
      screen.getByRole("link", { name: "How restricted access works (opens in a new tab)" }).getAttribute("href"),
    ).toBe("https://example.test/docs");
    const box = screen.getByRole("checkbox", { name: "Also read org/private" });
    expect(box).not.toBeChecked();
    expect(screen.queryByTestId("token-read-scope-included")).toBeNull();
    fireEvent.click(box);
    expect(onToggle).toHaveBeenCalledWith(scope.excludedRestricted[0]);
    rerender(
      <TokenReadScopePanel
        readsCollections
        scope={scope}
        included={new Set(["org/private"])}
        onToggleRepo={onToggle}
      />,
    );
    expect(screen.getByRole("checkbox", { name: "Also read org/private" })).toBeChecked();
    expect(screen.getByTestId("token-read-scope-included").textContent).toContain("org/private");
  });
});
