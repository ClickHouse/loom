import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import type { Dataset } from "../api";
import { toast } from "../components/Toast";
import { DatasetsDialog } from "./DatasetsDialog";

vi.mock("../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, getDatasets: vi.fn(), approveDataset: vi.fn(async () => ({ found: true, retyped: true })) };
});

const SET: Dataset = {
  dataset_id: "d1",
  name: "Revenue",
  sheet: "Q3",
  snapshot_id: "s1",
  schema_state: "needs_clarification",
  columns: [
    { name: "month", type: "string", inferred: "string" },
    { name: "amount", type: "number", inferred: "number" },
  ],
  clarifications: [{ code: "ambiguous_date", column: "month", detail: "could be a date" }],
  row_count: 12,
  rejected_rows: 1,
  approved_by: "",
};
const identity = { org: "acme", namespace: "team" };

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("DatasetsDialog", () => {
  it("shows a table's columns and questions, and approves it as typed", async () => {
    vi.mocked(api.getDatasets).mockResolvedValue({ found: true, datasets: [SET] });
    render(<DatasetsDialog identity={identity} sourceUri="q3.xlsx" canApprove onClose={() => {}} />);
    const dialog = await screen.findByRole("dialog", { name: "Tables" });
    expect(await within(dialog).findByText("Revenue")).toBeInTheDocument();
    expect(within(dialog).getByText("12 rows, 1 rejected")).toBeInTheDocument();
    expect(within(dialog).getByText(/could be a date/)).toBeInTheDocument();
    expect(within(dialog).getByText("needs a decision")).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Approve schema" }));
    await waitFor(() => expect(api.approveDataset).toHaveBeenCalledWith(identity, "q3.xlsx", SET, {}));
  });

  it("shows a reader the types without the approve", async () => {
    vi.mocked(api.getDatasets).mockResolvedValue({ found: true, datasets: [SET] });
    render(<DatasetsDialog identity={identity} sourceUri="q3.xlsx" canApprove={false} onClose={() => {}} />);
    expect(await screen.findByText("Revenue")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve schema" })).toBeNull();
  });

  it("says when the document became no table", async () => {
    vi.mocked(api.getDatasets).mockResolvedValue({ found: true, datasets: [] });
    render(<DatasetsDialog identity={identity} sourceUri="notes.md" canApprove onClose={() => {}} />);
    expect(await screen.findByText("No table was read from this document.")).toBeInTheDocument();
  });

  it("retypes the columns changed and approves", async () => {
    vi.mocked(api.getDatasets).mockResolvedValue({ found: true, datasets: [SET] });
    render(<DatasetsDialog identity={identity} sourceUri="q3.xlsx" canApprove onClose={() => {}} />);
    await screen.findByText("Revenue");
    fireEvent.click(screen.getByLabelText("Type of amount"));
    const options = screen.getAllByRole("dialog").at(-1) as HTMLElement;
    fireEvent.click(within(options).getByText("string"));
    await userEvent.click(screen.getByRole("button", { name: "Retype and approve" }));
    await waitFor(() =>
      expect(api.approveDataset).toHaveBeenCalledWith(identity, "q3.xlsx", SET, { amount: "string" }),
    );
  });

  it("says a table that changed meanwhile was not approved", async () => {
    vi.mocked(api.getDatasets).mockResolvedValue({ found: true, datasets: [SET] });
    vi.mocked(api.approveDataset).mockResolvedValueOnce({ found: false, retyped: false });
    render(<DatasetsDialog identity={identity} sourceUri="q3.xlsx" canApprove onClose={() => {}} />);
    await userEvent.click(await screen.findByRole("button", { name: "Approve schema" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Revenue changed meanwhile; read it again", "info"));
    expect(api.getDatasets).toHaveBeenCalledTimes(2);
  });

  it("offers no approve on an approved table until a column is retyped", async () => {
    const approved = { ...SET, schema_state: "approved", approved_by: "a@x", clarifications: [] };
    vi.mocked(api.getDatasets).mockResolvedValue({ found: true, datasets: [approved] });
    render(<DatasetsDialog identity={identity} sourceUri="q3.xlsx" canApprove onClose={() => {}} />);
    expect(await screen.findByText("approved by a@x")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve schema" })).toBeNull();
  });

  it("says when the document is gone", async () => {
    vi.mocked(api.getDatasets).mockResolvedValue({ found: false, datasets: [] });
    render(<DatasetsDialog identity={identity} sourceUri="q3.xlsx" canApprove onClose={() => {}} />);
    expect(await screen.findByText("This document is no longer here.")).toBeInTheDocument();
  });
});
