import { cleanup, fireEvent, render, screen, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry } from "../api";
import { useStore } from "../lib/state";
import { Browse } from "./Browse";

// "Namespaces you manage" lists memory namespaces only. The code indexes and tracker
// records someone manages each get a section of their own at the bottom, folded until
// opened (remembered), with a way to the Repositories page; a section with nothing in it
// is not drawn.

const admin = (namespace: string, kind: MyNamespaceEntry["kind"], description = ""): MyNamespaceEntry => ({
  namespace,
  role: "admin",
  status: "active",
  visibility: "private",
  description,
  joined_at: null,
  is_new: false,
  kind,
});

let mine: MyNamespaceEntry[] = [];
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, browseNamespaces: vi.fn(async () => []) };
});
vi.mock("../lib/sharedReads", () => ({ sharedMine: () => Promise.resolve(mine) }));

describe("Browse: code indexes you manage", () => {
  beforeEach(() => {
    localStorage.clear();
    useStore.setState({
      authed: true,
      whoami: { authenticated: true, subject: "alice", org: "acme" },
      identity: { org: "acme", namespace: "team", agent: "loom-ui" },
      view: "browse",
    });
  });
  afterEach(() => cleanup());

  it("keeps code indexes out of the memory section and folds them into their own", async () => {
    mine = [
      admin("team", "memory"),
      admin("code-ch", "code", "ClickHouse/ClickHouse @ master"),
      admin("code-loom", "code", "acme/loom @ main"),
    ];
    render(<Browse />);
    const memory = (await screen.findByRole("heading", { name: /Namespaces you manage/ })).closest("section")!;
    expect(within(memory).getByText("team")).toBeInTheDocument();
    expect(within(memory).queryByText("code-ch")).toBeNull();

    const fold = screen.getByRole("button", { name: "Repositories you manage" });
    expect(fold).toHaveAttribute("aria-expanded", "false");
    expect(fold.closest("section")).toHaveTextContent("2");
    expect(screen.queryByText("ClickHouse/ClickHouse @ master")).toBeNull();

    fireEvent.click(fold);
    expect(fold).toHaveAttribute("aria-expanded", "true");
    const body = document.getElementById(fold.getAttribute("aria-controls") ?? "")!;
    // The repository is the row's first line, the namespace its second, as code.
    const repo = within(body).getByText("ClickHouse/ClickHouse @ master");
    expect(within(body).getByText("code-ch").tagName).toBe("CODE");
    expect(
      repo.compareDocumentPosition(within(body).getByText("code-ch")) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(within(body).getByRole("button", { name: "Invite people to code-ch" })).toBeInTheDocument();
    expect(within(body).getByRole("button", { name: "Actions for code-ch" })).toBeInTheDocument();
    fireEvent.click(within(body).getByRole("button", { name: "Manage indexing in Repositories" }));
    expect(useStore.getState().view).toBe("code");
    // No tracker record managed: no tracker section.
    expect(screen.queryByRole("button", { name: "Tracker records you manage" })).toBeNull();
  });

  it("remembers that the section was opened", async () => {
    mine = [admin("code-ch", "code", "ClickHouse/ClickHouse @ master")];
    const first = render(<Browse />);
    fireEvent.click(await screen.findByRole("button", { name: "Repositories you manage" }));
    first.unmount();
    render(<Browse />);
    expect(await screen.findByRole("button", { name: "Repositories you manage" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );
  });

  it("gives tracker records the same folded section, and hides empty ones", async () => {
    mine = [admin("tracker-ch", "qa", "ClickHouse issues")];
    render(<Browse />);
    expect(await screen.findByRole("button", { name: "Tracker records you manage" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
    expect(screen.queryByRole("button", { name: "Repositories you manage" })).toBeNull();
    expect(screen.queryByRole("heading", { name: /Namespaces you manage/ })).toBeNull();
  });
});
