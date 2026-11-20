import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { MyNamespaceEntry } from "../api";
import { CopyToNamespaceDialog } from "./CopyToNamespaceDialog";

// A copy goes to another namespace you write: never a repository, never a collection.

const entry = (namespace: string, over: Partial<MyNamespaceEntry> = {}): MyNamespaceEntry => ({
  namespace,
  role: "writer",
  status: "active",
  visibility: "private",
  description: "",
  joined_at: null,
  is_new: false,
  kind: "memory",
  ...over,
});

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    listMine: vi.fn(async () => [
      entry("team"),
      entry("ops"),
      entry("col-r", { kind: "collection", title: "Research" }),
      entry("code-x", { kind: "code" }),
    ]),
  };
});
import { listMine } from "../api";

afterEach(() => cleanup());

describe("CopyToNamespaceDialog", () => {
  it("offers writable namespaces only: no collection, no repository", async () => {
    render(
      <CopyToNamespaceDialog
        open
        onOpenChange={() => {}}
        source={{ org: "dev", namespace: "team", agent: "loom-ui" }}
        memoryKey="k"
      />,
    );
    await waitFor(() => expect(listMine).toHaveBeenCalled());
    const select = await screen.findByLabelText(/Destination namespace/);
    await waitFor(() => expect(select).not.toHaveAttribute("data-disabled"));
    await userEvent.click(select);
    const options = (await screen.findAllByRole("dialog")).at(-1) as HTMLElement;
    expect(await within(options).findByText("ops · writer")).toBeInTheDocument();
    expect(within(options).queryByText(/col-r|Research/)).toBeNull();
    expect(within(options).queryByText(/code-x/)).toBeNull();
  });
});
