import { cleanup, render, screen, waitFor } from "../../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../api/guardrails";
import type { ExtractionPolicy, OrgModelCalls } from "../../api/guardrails";
import { confirmAction } from "../../components/ConfirmDialog";
import { useStore } from "../../lib/state";
import { OrgGuardrails } from "./OrgGuardrails";

vi.mock("../../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock("../../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../../api/guardrails", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api/guardrails")>();
  return {
    ...actual,
    getExtractionPolicy: vi.fn(),
    setExtractionPolicy: vi.fn(),
    clearExtractionPolicy: vi.fn(),
    getOrgModelCalls: vi.fn(),
    setOrgModelCalls: vi.fn(),
  };
});

const policy = (scope: "org" | "namespace", text: string): ExtractionPolicy => ({
  scope,
  namespace: scope === "org" ? "" : "team",
  policy: text,
  updated_by: text ? "alice" : "",
  updated_at: null,
  effective: text,
});
const CALLS: OrgModelCalls = {
  enabled: true,
  reason: "",
  updated_by: "",
  updated_at: null,
  locked: false,
  locked_by: "",
};
const scope = { org: "acme", namespace: "team" };

beforeEach(() => {
  useStore.setState({ identity: { ...scope, agent: "loom-ui" } });
  vi.mocked(api.getExtractionPolicy).mockImplementation(async (_id, s) =>
    policy(s, s === "org" ? "Never store card numbers." : ""),
  );
  vi.mocked(api.getOrgModelCalls).mockResolvedValue(CALLS);
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("OrgGuardrails", () => {
  it("saves the namespace's rules and removes the org's", async () => {
    vi.mocked(api.setExtractionPolicy).mockResolvedValue(policy("namespace", "No health data."));
    vi.mocked(api.clearExtractionPolicy).mockResolvedValue(policy("org", ""));
    render(<OrgGuardrails />);
    const fields = await screen.findAllByLabelText("Rules");
    expect(fields[0]).toHaveValue("Never store card numbers.");
    await userEvent.type(fields[1], "No health data.");
    await userEvent.click(screen.getAllByRole("button", { name: "Save rules" })[1]);
    await waitFor(() =>
      expect(api.setExtractionPolicy).toHaveBeenCalledWith(
        expect.objectContaining(scope),
        "namespace",
        "No health data.",
      ),
    );
    // The org's rules are set, so its panel offers Remove; the namespace's only once saved.
    await userEvent.click(screen.getAllByRole("button", { name: "Remove" })[0]);
    await waitFor(() => expect(api.clearExtractionPolicy).toHaveBeenCalledWith(expect.objectContaining(scope), "org"));
  });

  it("switches the org's model calls off only after a confirmation", async () => {
    vi.mocked(api.setOrgModelCalls).mockResolvedValue({ ...CALLS, enabled: false });
    render(<OrgGuardrails />);
    await userEvent.click(await screen.findByRole("switch", { name: "Model calls" }));
    expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({ danger: true }));
    await waitFor(() => expect(api.setOrgModelCalls).toHaveBeenCalledWith("acme", false, ""));
    expect(await screen.findByText("Off")).toBeInTheDocument();
  });

  it("holds the switch under a platform admin's lock", async () => {
    vi.mocked(api.getOrgModelCalls).mockResolvedValue({ ...CALLS, enabled: false, locked: true, locked_by: "root" });
    render(<OrgGuardrails />);
    expect(await screen.findByRole("switch", { name: "Model calls" })).toBeDisabled();
    expect(screen.getByText(/only they can switch them on/)).toBeInTheDocument();
  });
});
