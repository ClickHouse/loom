/** The Memories page names the namespace's guide: its summary and who changed it last for
 *  anyone, an invitation to write one for a writer, and nothing for a reader when there is
 *  none. Writing the first one opens straight on the editor, from a starter outline. */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "../test/rtl";
import { NamespaceGuideCard } from "./NamespaceGuideCard";
import * as api from "../api";
import { useNamespaceRole } from "../lib/namespaceRole";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, getNamespaceGuide: vi.fn(), saveNamespaceGuide: vi.fn() };
});
vi.mock("../lib/namespaceRole", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/namespaceRole")>();
  return { ...actual, useNamespaceRole: vi.fn() };
});

const NONE: api.NamespaceGuide = {
  found: false,
  revision: 0,
  content: "",
  summary: "",
  note: "",
  updated_by: "",
  updated_at: null,
};
const GUIDE: api.NamespaceGuide = {
  ...NONE,
  found: true,
  revision: 3,
  content: "## Keys\n- acct/<id>/plan",
  summary: "Per-account plans",
  updated_by: "human:alice",
  updated_at: "2026-10-21T10:00:00Z",
};
const identity = { org: "o", namespace: "billing" };

beforeEach(() => {
  vi.mocked(api.getNamespaceGuide).mockReset();
  vi.mocked(useNamespaceRole).mockReturnValue("writer");
});

describe("NamespaceGuideCard", () => {
  it("shows the guide's summary and who changed it, and opens it", async () => {
    vi.mocked(api.getNamespaceGuide).mockResolvedValue(GUIDE);
    vi.mocked(useNamespaceRole).mockReturnValue("viewer");
    render(<NamespaceGuideCard identity={identity} />);
    expect(await screen.findByText(/Per-account plans/)).toBeInTheDocument();
    expect(screen.getByText(/Revision 3 by human:alice/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open guide" }));
    expect(await screen.findByRole("dialog", { name: "Guide for billing" })).toBeInTheDocument();
  });

  it("asks a writer to write the first guide, and opens the editor on an outline", async () => {
    vi.mocked(api.getNamespaceGuide).mockResolvedValue(NONE);
    render(<NamespaceGuideCard identity={identity} />);
    fireEvent.click(await screen.findByRole("button", { name: "Write the guide" }));
    const editor = await screen.findByLabelText("Guide (Markdown)");
    expect((editor as HTMLTextAreaElement).value).toContain("## What this namespace holds");
  });

  it("shows a reader nothing when there is no guide", async () => {
    vi.mocked(api.getNamespaceGuide).mockResolvedValue(NONE);
    vi.mocked(useNamespaceRole).mockReturnValue("viewer");
    const { container } = render(<NamespaceGuideCard identity={identity} />);
    await vi.waitFor(() => expect(api.getNamespaceGuide).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("shows nothing when the guide cannot be read", async () => {
    vi.mocked(api.getNamespaceGuide).mockRejectedValue(new Error("down"));
    const { container } = render(<NamespaceGuideCard identity={identity} />);
    await vi.waitFor(() => expect(api.getNamespaceGuide).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
