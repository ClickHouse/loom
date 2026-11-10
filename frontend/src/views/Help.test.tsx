import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { HelpPage } from "../api/help";
import { useStore } from "../lib/state";

const helpPage = vi.fn<(path: string) => Promise<HelpPage>>();
vi.mock("../api/help", () => ({ helpPage: (path: string) => helpPage(path) }));

const { Help } = await import("./Help");

function page(path: string, text: string, over: Partial<HelpPage> = {}): HelpPage {
  return {
    available: true,
    found: true,
    path,
    title: "Tokens and grants",
    section: "",
    anchor: "",
    audience: "user",
    text,
    truncated: false,
    next_offset: 0,
    sections: [
      { section: "Tokens and grants", anchor: "tokens-and-grants", heading: "Tokens and grants" },
      { section: "Tokens and grants > Revoke and rotate", anchor: "revoke-and-rotate", heading: "Revoke and rotate" },
    ],
    ...over,
  };
}

describe("Help", () => {
  beforeEach(() => {
    helpPage.mockReset();
    Element.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("reads the page the URL names and scrolls to the cited section", async () => {
    window.history.replaceState(null, "", "/help?page=guides%2Ftokens-and-grants.md#revoke-and-rotate");
    helpPage.mockResolvedValue(
      page(
        "guides/tokens-and-grants.md",
        "# Tokens and grants\n\nSee [the CLI](../reference/cli.md).\n\n## Revoke and rotate\n\nRevoke.",
      ),
    );
    render(<Help />);
    const heading = await screen.findByRole("heading", { name: "Revoke and rotate" });
    expect(helpPage).toHaveBeenCalledWith("guides/tokens-and-grants.md");
    expect(heading).toHaveAttribute("id", "revoke-and-rotate");
    // The scroll runs in an effect after the render that draws the heading.
    await waitFor(() => expect(heading.scrollIntoView).toHaveBeenCalled());
  });

  it("follows a link to another page without leaving the view", async () => {
    window.history.replaceState(null, "", "/help?page=guides%2Ftokens-and-grants.md");
    helpPage.mockImplementation(async (path) =>
      path === "reference/cli.md"
        ? page(path, "# CLI reference\n\nCommands.", { title: "CLI reference", sections: [] })
        : page(path, "# Tokens and grants\n\nSee [the CLI](../reference/cli.md)."),
    );
    render(<Help />);
    await userEvent.setup().click(await screen.findByRole("link", { name: "the CLI" }));
    expect(await screen.findByRole("heading", { name: "CLI reference" })).toBeInTheDocument();
    expect(window.location.search).toBe("?page=reference%2Fcli.md");
  });

  it("scrolls again when a link opens another section of the page already shown", async () => {
    window.history.replaceState(null, "", "/help?page=guides%2Ftokens-and-grants.md");
    helpPage.mockResolvedValue(
      page("guides/tokens-and-grants.md", "# Tokens and grants\n\nIntro.\n\n## Revoke and rotate\n\nRevoke."),
    );
    render(<Help />);
    const heading = await screen.findByRole("heading", { name: "Revoke and rotate" });
    expect(heading.scrollIntoView).not.toHaveBeenCalled();
    await act(async () => {
      useStore.getState().openDocsPage("guides/tokens-and-grants.md", "revoke-and-rotate");
    });
    await waitFor(() => expect(heading.scrollIntoView).toHaveBeenCalled());
    expect(helpPage).toHaveBeenCalledTimes(1);
  });

  it("looks up an anchor that is no valid escape as written", async () => {
    window.history.replaceState(null, "", "/help?page=guides%2Ftokens-and-grants.md#50%");
    helpPage.mockResolvedValue(page("guides/tokens-and-grants.md", "# Tokens and grants\n\nIntro."));
    render(<Help />);
    expect(await screen.findByText("Intro.")).toBeInTheDocument();
  });

  it("says when the build has no such page, or no docs", async () => {
    window.history.replaceState(null, "", "/help?page=guides%2Fabsent.md");
    helpPage.mockResolvedValue({ ...page("guides/absent.md", ""), found: false });
    const { unmount } = render(<Help />);
    expect(await screen.findByText("This build's documentation has no page guides/absent.md.")).toBeInTheDocument();
    unmount();
    helpPage.mockResolvedValue({ ...page("index.md", ""), found: false, available: false });
    render(<Help />);
    expect(await screen.findByText("This build of Loom ships no documentation to read.")).toBeInTheDocument();
  });
});
