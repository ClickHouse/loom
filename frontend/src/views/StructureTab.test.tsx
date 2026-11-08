import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/structure";
import type { MyNamespaceEntry, WhoAmI } from "../api";
import { useStore } from "../lib/state";
import { resetNamespaceRoleCache } from "../lib/namespaceRole";
import { StructureTab } from "./StructureTab";

vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
import { confirmAction } from "../components/ConfirmDialog";
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
      // Accepting sends its description and cardinality back, so they are kept.
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
    await waitFor(() =>
      expect(api.recordHistory).toHaveBeenCalledWith(expect.objectContaining(scope), "r-old", expect.anything()),
    );
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

  it("does not register a listed category again, since that would blank its description", async () => {
    signIn("writer");
    render(<StructureTab />);
    await userEvent.type(await screen.findByLabelText("Name", { selector: "#category-name" }), "Billing");
    expect(screen.getAllByText("Already listed").length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "Add category" })).toBeDisabled();
  });

  it("warns before a new schema replaces one of the same name, and drops a field row", async () => {
    signIn("admin");
    render(<StructureTab />);
    await userEvent.click(await screen.findByRole("button", { name: "New schema" }));
    const dialog = await screen.findByRole("dialog", { name: "New record schema" });
    await userEvent.type(within(dialog).getByLabelText("Name"), "invoice");
    expect(within(dialog).getByText("invoice exists: registering replaces its fields.")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Add a field" }));
    expect(within(dialog).getByLabelText("Field 2")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove field 2" }));
    expect(within(dialog).queryByLabelText("Field 2")).toBeNull();
  });

  it("adds an unlisted term with no description or cardinality, and clears the field", async () => {
    signIn("admin");
    render(<StructureTab />);
    const field = await screen.findByLabelText("Name", { selector: "#ontology-name" });
    await userEvent.type(field, "Vendor");
    await userEvent.click(screen.getByRole("button", { name: "Add term" }));
    await waitFor(() =>
      expect(api.registerOntologyTerm).toHaveBeenCalledWith(expect.objectContaining(scope), {
        kind: "entity_type",
        name: "Vendor",
        description: "",
        cardinality: "",
      }),
    );
    await waitFor(() => expect(field).toHaveValue(""));
  });

  it("asks before replacing a profile schema that exists, and refuses JSON that is not an object", async () => {
    signIn("admin");
    vi.mocked(api.getProfileSchema).mockResolvedValueOnce({
      found: true,
      json_schema: { type: "object", properties: {} },
      updated_by: "a",
      updated_at: null,
    });
    render(<StructureTab />);
    const field = await screen.findByLabelText("JSON Schema");
    await userEvent.clear(field);
    await userEvent.click(field);
    await userEvent.paste("[]");
    expect(screen.getByText("Must be a JSON object")).toBeInTheDocument();
    await userEvent.clear(field);
    await userEvent.click(field);
    await userEvent.paste('{"type": "object", "properties": {"a": {"type": "string", "description": "x"}}}');
    await userEvent.click(screen.getByRole("button", { name: "Save schema" }));
    await waitFor(() =>
      expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({ title: "Replace the profile schema?" })),
    );
  });

  it("holds the other prior records while one is read", async () => {
    signIn("viewer");
    const conflict = (key: string, prior: string) => ({
      memory_key: key,
      field_name: "amount",
      prior_value: 1,
      incoming_value: 2,
      prior_record_id: prior,
      incoming_record_id: `${prior}-new`,
      detected_at: null,
    });
    vi.mocked(api.recordConflicts).mockResolvedValueOnce({
      truncated: false,
      rows: [conflict("inv/A-1", "r-a"), conflict("inv/B-2", "r-b")],
    });
    vi.mocked(api.recordHistory).mockImplementationOnce(() => new Promise(() => {}));
    render(<StructureTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Records" }));
    await userEvent.click(await screen.findByRole("button", { name: "Prior record of inv/A-1" }));
    expect(screen.getByRole("button", { name: "Prior record of inv/B-2" })).toBeDisabled();
  });

  it("keeps the records when the disagreements fail, with a Retry of their own", async () => {
    signIn("viewer");
    vi.mocked(api.recordConflicts).mockRejectedValueOnce(new Error("down"));
    render(<StructureTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Records" }));
    const dialog = await screen.findByRole("dialog", { name: "Records of invoice" });
    expect(await within(dialog).findByText("A-1")).toBeInTheDocument();
    await userEvent.click(await within(dialog).findByRole("button", { name: "Retry" }));
    await waitFor(() => expect(api.recordConflicts).toHaveBeenCalledTimes(2));
  });

  it("lists records by key in the schema's field order, then keys an older field set left", async () => {
    signIn("viewer");
    vi.mocked(api.queryRecords).mockResolvedValueOnce({
      truncated: false,
      rows: [
        {
          record_id: "r1",
          memory_key: "inv/1",
          record: { legacy: "x", number: "A-1" },
          status: "active",
          valid_at: null,
          valid_to: null,
        },
      ],
    });
    render(<StructureTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Records" }));
    const table = await screen.findByRole("table", { name: "Records of invoice" });
    const headers = within(table)
      .getAllByRole("columnheader")
      .map((h) => h.textContent);
    expect(headers).toEqual(["Key", "number", "legacy"]);
    expect(within(table).getByText("inv/1")).toBeInTheDocument();
  });

  it("aborts the prior record's read when the dialog closes", async () => {
    signIn("viewer");
    vi.mocked(api.recordConflicts).mockResolvedValueOnce({
      truncated: false,
      rows: [
        {
          memory_key: "inv/A-1",
          field_name: "amount",
          prior_value: 1,
          incoming_value: 2,
          prior_record_id: "r-a",
          incoming_record_id: "r-a2",
          detected_at: null,
        },
      ],
    });
    let signal: AbortSignal | undefined;
    vi.mocked(api.recordHistory).mockImplementationOnce((_id, _record, opts) => {
      signal = opts?.signal;
      return new Promise(() => {});
    });
    render(<StructureTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Records" }));
    const dialog = await screen.findByRole("dialog", { name: "Records of invoice" });
    await userEvent.click(await within(dialog).findByRole("button", { name: "Prior record of inv/A-1" }));
    await userEvent.click(within(dialog).getAllByRole("button", { name: "Close" }).at(-1) as HTMLElement);
    await waitFor(() => expect(signal?.aborted).toBe(true));
  });
});
