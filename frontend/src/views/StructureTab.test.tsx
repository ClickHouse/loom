import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/structure";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { StructureTab } from "./StructureTab";

vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock("../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../api/structure", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/structure")>();
  return {
    ...actual,
    listRecordSchemas: vi.fn(async () => ({
      schemas: [
        {
          schema_id: "s1",
          name: "invoice",
          description: "",
          status: "active",
          fields: [{ name: "number", type: "string", required: true, identity: true }],
        },
        { schema_id: "s2", name: "lead", description: "", status: "proposed", fields: [] },
      ],
    })),
    registerRecordSchema: vi.fn(async () => ({ name: "ticket", status: "active" })),
    decideRecordSchema: vi.fn(async () => ({ status: "active", updated: true })),
    queryRecords: vi.fn(async () => ({
      truncated: false,
      rows: [
        {
          record_id: "r1",
          memory_key: "inv-1",
          record: { number: "A-1" },
          status: "active",
          valid_at: null,
          valid_to: null,
        },
      ],
    })),
    recordHistory: vi.fn(async () => ({ rows: [] })),
    recordConflicts: vi.fn(async () => ({ rows: [] })),
    getProfileSchema: vi.fn(async () => ({ found: false, json_schema: {}, updated_by: "", updated_at: null })),
    setProfileSchema: vi.fn(async () => ({ found: true, json_schema: {}, updated_by: "a", updated_at: null })),
    clearProfileSchema: vi.fn(),
    listOntology: vi.fn(async () => [{ kind: "entity_type", name: "Customer", status: "pending", description: "" }]),
    registerOntologyTerm: vi.fn(async () => ({ registered: true })),
    listCategories: vi.fn(async () => [{ category_id: "c1", name: "Billing", description: "", item_count: 4 }]),
    registerCategory: vi.fn(async () => ({ category_id: "c2" })),
  };
});

const scope = { org: "acme", namespace: "team" };
function signIn(role: MyNamespaceEntry["role"]) {
  const whoami: WhoAmI = { authenticated: true, subject: "a@x", org: "acme", roles: ["writer"], namespaces: ["team"] };
  useStore.setState({
    whoami,
    myNamespaces: [{ namespace: "team", role, status: "active" } as MyNamespaceEntry],
    identity: { ...scope, agent: "loom-ui" },
  });
}

beforeEach(() => resetNamespaceRoleCache());
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useStore.setState({ whoami: null, myNamespaces: null, identity: null });
});

describe("StructureTab", () => {
  it("shows a viewer the schemas, records, ontology and categories, with no change offered", async () => {
    signIn("viewer");
    render(<StructureTab />);
    expect(await screen.findByText("invoice")).toBeInTheDocument();
    expect(screen.getByText("Billing")).toBeInTheDocument();
    expect(screen.getByText("Customer")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New schema" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept Customer" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add category" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Records" }));
    const dialog = await screen.findByRole("dialog", { name: "Records of invoice" });
    expect(await within(dialog).findByText("A-1")).toBeInTheDocument();
  });

  it("lets an admin approve a proposed schema, accept a pending term and register a schema", async () => {
    signIn("admin");
    render(<StructureTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() =>
      expect(api.decideRecordSchema).toHaveBeenCalledWith(expect.objectContaining(scope), "lead", "approve"),
    );
    await userEvent.click(screen.getByRole("button", { name: "Accept Customer" }));
    await waitFor(() =>
      expect(api.registerOntologyTerm).toHaveBeenCalledWith(
        expect.objectContaining(scope),
        "entity_type",
        "Customer",
        "",
      ),
    );
    await userEvent.click(screen.getByRole("button", { name: "New schema" }));
    const dialog = await screen.findByRole("dialog", { name: "New record schema" });
    await userEvent.type(within(dialog).getByLabelText("Name"), "ticket");
    await userEvent.type(within(dialog).getByLabelText("Field 1"), "id");
    await userEvent.click(within(dialog).getByRole("checkbox", { name: "Identity" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Register" }));
    await waitFor(() =>
      expect(api.registerRecordSchema).toHaveBeenCalledWith(expect.objectContaining(scope), "ticket", "", [
        { name: "id", type: "string", required: false, identity: true },
      ]),
    );
  });

  it("saves a profile schema only as valid JSON", async () => {
    signIn("admin");
    render(<StructureTab />);
    const field = await screen.findByLabelText("JSON Schema");
    await userEvent.type(field, "{{not json");
    expect(screen.getByText("Not valid JSON")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save schema" })).toBeDisabled();
    await userEvent.clear(field);
    await userEvent.type(field, '{{"type": "object"}');
    await userEvent.click(screen.getByRole("button", { name: "Save schema" }));
    await waitFor(() =>
      expect(api.setProfileSchema).toHaveBeenCalledWith(expect.objectContaining(scope), { type: "object" }),
    );
  });

  it("lets a writer add a category", async () => {
    signIn("writer");
    render(<StructureTab />);
    await userEvent.type(await screen.findByLabelText("Name"), "Legal");
    await userEvent.click(screen.getByRole("button", { name: "Add category" }));
    await waitFor(() => expect(api.registerCategory).toHaveBeenCalledWith(expect.objectContaining(scope), "Legal", ""));
  });
});
