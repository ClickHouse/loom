import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
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
    listOntology: vi.fn(async () => [
      { kind: "relation_verb", name: "works_at", status: "pending", description: "employer", cardinality: "one" },
    ]),
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
    expect(screen.getByText("works_at")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New schema" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept works_at" })).toBeNull();
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
    await userEvent.click(screen.getByRole("button", { name: "Accept works_at" }));
    await waitFor(() =>
      // The whole entry goes back, so accepting keeps its cardinality and description.
      expect(api.registerOntologyTerm).toHaveBeenCalledWith(
        expect.objectContaining(scope),
        expect.objectContaining({
          kind: "relation_verb",
          name: "works_at",
          description: "employer",
          cardinality: "one",
        }),
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
    const valid = { type: "object", properties: { timezone: { type: "string", description: "IANA zone" } } };
    await userEvent.click(field);
    await userEvent.paste(JSON.stringify(valid));
    await userEvent.click(screen.getByRole("button", { name: "Save schema" }));
    await waitFor(() => expect(api.setProfileSchema).toHaveBeenCalledWith(expect.objectContaining(scope), valid));
  });

  it("lets a writer add a category", async () => {
    signIn("writer");
    render(<StructureTab />);
    await userEvent.type(await screen.findByLabelText("Name"), "Legal");
    await userEvent.click(screen.getByRole("button", { name: "Add category" }));
    await waitFor(() => expect(api.registerCategory).toHaveBeenCalledWith(expect.objectContaining(scope), "Legal", ""));
  });

  it("opens the prior record a disagreement superseded", async () => {
    signIn("viewer");
    vi.mocked(api.recordConflicts).mockResolvedValueOnce({
      truncated: false,
      rows: [
        {
          memory_key: "inv/A-1",
          field_name: "amount",
          prior_value: 10,
          incoming_value: 12,
          prior_record_id: "r-old",
          incoming_record_id: "r-new",
          detected_at: null,
        },
      ],
    });
    vi.mocked(api.recordHistory).mockResolvedValueOnce({
      rows: [
        {
          record_id: "r-old",
          memory_key: "inv/A-1",
          record: { amount: 10 },
          status: "superseded",
          valid_at: null,
          valid_to: null,
        },
      ],
    });
    render(<StructureTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Records" }));
    await userEvent.click(await screen.findByRole("button", { name: "Prior record of inv/A-1" }));
    await waitFor(() => expect(api.recordHistory).toHaveBeenCalledWith(expect.objectContaining(scope), "r-old"));
    expect(await screen.findByText(/superseded · \{"amount":10\}/)).toBeInTheDocument();
  });

  it("refuses to add a term already listed, and holds Approve while it runs", async () => {
    signIn("admin");
    vi.mocked(api.decideRecordSchema).mockImplementationOnce(() => new Promise(() => {}));
    render(<StructureTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Kind of term"));
    fireEvent.click(within(screen.getAllByRole("dialog").at(-1) as HTMLElement).getByText("Relation"));
    await userEvent.type(screen.getByLabelText("Name", { selector: "#ontology-name" }), "works_at");
    expect(screen.getByText("Already listed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add term" })).toBeDisabled();
  });
});
