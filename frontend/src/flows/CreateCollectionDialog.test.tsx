/**
 * New collection: a person types a title and the server derives the address; a title
 * already used there opens that collection; a refused address says why and offers a free
 * one.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  createCollection: vi.fn(),
}));
vi.mock("../components/Toast", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../components/Toast")>()),
  toast: vi.fn(),
}));
import { createCollection, type CollectionEntry } from "../api";
import { toast } from "../components/Toast";
import { placeholderName, plainName, titleKey } from "../lib/collections";
import { ApiError } from "../lib/errors";
import { CreateCollectionDialog } from "./CreateCollectionDialog";

const create = vi.mocked(createCollection);

const parent = (namespace: string, title = ""): CollectionEntry => ({
  namespace,
  parent: "",
  personal: false,
  role: "writer",
  title,
});

function mount(onCreated = vi.fn(), parents: CollectionEntry[] = [], initialParent = "") {
  render(
    <CreateCollectionDialog
      open
      onOpenChange={() => {}}
      org="acme"
      parents={parents}
      initialParent={initialParent}
      onCreated={onCreated}
    />,
  );
  return onCreated;
}

function type(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

describe("CreateCollectionDialog", () => {
  beforeEach(() => {
    create.mockReset();
    vi.mocked(toast).mockReset();
  });
  afterEach(cleanup);

  it("creates from a title, saying the address it derives", async () => {
    create.mockResolvedValue({ namespace: "col-q3-plans", parent: "col-team", created: true, title: "Q3 Plans" });
    const onCreated = mount(vi.fn(), [parent("col-team", "Team")], "col-team");
    type("Title", "Q3 Plans");
    expect(screen.getByText(/Its address is col-q3-plans, with a short suffix if that is taken/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("col-q3-plans"));
    expect(create).toHaveBeenCalledWith("acme", "col-q3-plans", {
      parent: "col-team",
      description: "",
      title: "Q3 Plans",
      deriveName: true,
    });
    expect(toast).toHaveBeenCalledWith("Created Q3 Plans", "ok");
  });

  it("says a top-level collection belongs to the org, not to the header's namespace", () => {
    render(
      <CreateCollectionDialog
        open
        onOpenChange={() => {}}
        org="acme"
        parents={[parent("col-team", "Team")]}
        headerNamespace="research"
        onCreated={vi.fn()}
      />,
    );
    const hint = document.getElementById("create-collection-parent-hint");
    expect(hint?.textContent).toContain("A top-level collection belongs to the org, not to research");
    cleanup();
    // No namespace in the header: the org alone.
    mount(vi.fn(), [parent("col-team", "Team")]);
    expect(document.getElementById("create-collection-parent-hint")?.textContent).toContain(
      "A top-level collection belongs to the org: only the members you add read it.",
    );
    cleanup();
    mount(vi.fn(), [parent("col-team", "Team")], "col-team");
    expect(document.getElementById("create-collection-parent-hint")?.textContent).toContain(
      "A collection inside another inherits its grants.",
    );
  });

  it("opens the collection a title already names there", async () => {
    create.mockResolvedValue({ namespace: "col-docs", parent: "", created: false, title: "Docs" });
    const onCreated = mount();
    type("Title", "docs");
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith("col-docs"));
    expect(toast).toHaveBeenCalledWith("Docs already exists here", "info");
  });

  it("says why an address is refused and offers a free one", async () => {
    create.mockRejectedValueOnce(
      new ApiError({
        status: 409,
        code: "COLLECTION_NAME_TAKEN",
        message: "the name 'col-x' belonged to a deleted collection in acme: a deleted name is never used again",
        detail: { deleted: true, suggestion: "col-x-ab12" },
      }),
    );
    mount();
    type("Title", "X");
    fireEvent.click(screen.getByLabelText("Choose the address myself"));
    type("Address", "col-x");
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Name taken: .*deleted collection.*never used again/);
    fireEvent.click(screen.getByRole("button", { name: "Use col-x-ab12" }));
    expect(screen.getByLabelText("Address")).toHaveValue("col-x-ab12");
    expect(create).toHaveBeenCalledWith("acme", "col-x", { parent: "", description: "", title: "X" });
  });

  it("counts a title's length as the server does, after joining accents", () => {
    mount();
    type("Title", "e\u0301".repeat(150)); // 300 code points, 150 characters
    expect(screen.queryByText("At most 200 characters.")).toBeNull();
  });

  it("needs a title unless the address is chosen", () => {
    mount();
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
    type("Title", "   ");
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();
    type("Title", "Plans");
    expect(screen.getByRole("button", { name: "Create" })).toBeEnabled();
  });
});

describe("placeholderName", () => {
  it("names a title with nothing to spell without WebCrypto, one name per title", () => {
    const subtle = Object.getOwnPropertyDescriptor(globalThis.crypto, "subtle");
    Object.defineProperty(globalThis.crypto, "subtle", { value: undefined, configurable: true });
    try {
      const a = placeholderName("日本");
      expect(a).toMatch(/^col-c-[0-9a-z]{6}$/);
      expect(placeholderName("日本")).toBe(a);
      expect(placeholderName("中国")).not.toBe(a);
    } finally {
      if (subtle) Object.defineProperty(globalThis.crypto, "subtle", subtle);
    }
  });
});

describe("titleKey", () => {
  it("compares titles as the server's title_key does", () => {
    expect(titleKey("Q3 Pl\u00adans")).toBe(titleKey("q3  plans")); // a soft hyphen is ignored
    expect(titleKey("Cafe\u0301")).toBe(titleKey("Caf\u00e9")); // one spelling of an accent
    expect(titleKey("Stra\u00dfe")).toBe(titleKey("STRASSE")); // casefold, not lower case
    expect(titleKey("Pay\u200droll")).not.toBe(titleKey("Payroll")); // a joiner counts
    expect(titleKey("\u{1f3f4}\u{e0067}\u{e0062}\u{e007f}")).not.toBe(titleKey("\u{1f3f4}")); // tags count
  });
});

describe("plainName", () => {
  it("derives the name the server tries first", () => {
    expect(plainName("Café Notes")).toBe("col-cafe-notes");
    expect(plainName("Straße")).toBe("col-strasse"); // as the server folds case
    expect(plainName("Kırmızı")).toBe("col-kirmizi");
    expect(plainName("  Q3 / Plans  ")).toBe("col-q3-plans");
    expect(plainName("Me")).toBe("col-c-me");
    expect(plainName("日本")).toBe("");
    expect(plainName(`${"a".repeat(39)} b`)).toBe(`col-${"a".repeat(39)}`);
  });
});
