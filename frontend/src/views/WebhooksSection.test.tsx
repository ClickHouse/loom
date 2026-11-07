import { cleanup, render, screen, waitFor, within } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api/webhooks";
import type { Webhook } from "../api/webhooks";
import { confirmAction } from "../components/ConfirmDialog";
import { toast } from "../components/Toast";
import { useStore } from "../lib/state";
import { WebhooksSection } from "./WebhooksSection";

vi.mock("../components/ConfirmDialog", () => ({ confirmAction: vi.fn(async () => true) }));
vi.mock("../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../api/webhooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/webhooks")>();
  return {
    ...actual,
    listWebhooks: vi.fn(),
    registerWebhook: vi.fn(async () => ({})),
    removeWebhook: vi.fn(async () => ({ removed: true })),
    listWebhookDeliveries: vi.fn(async () => ({
      deliveries: [
        {
          event_id: "e1",
          event_type: "memory.written",
          memory_key: "deploy_window",
          occurred_at: "2000-01-01 00:00:00",
          status: "dead",
          attempts: 6,
          last_status: 500,
          last_error: "",
        },
      ],
    })),
  };
});

const HOOK: Webhook = {
  webhook_id: "w1",
  url: "https://hooks.example.com/loom",
  secret_ref: "LOOM_WEBHOOK_SECRET_ACME__CI",
  events: [...api.WEBHOOK_EVENTS],
  created_at: "2000-01-01 00:00:00",
  secret_present: false,
};
const scope = { org: "acme", namespace: "team" };

beforeEach(() => {
  useStore.setState({ identity: { ...scope, agent: "loom-ui" } });
  vi.mocked(api.listWebhooks).mockResolvedValue({ webhooks: [HOOK] });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("WebhooksSection", () => {
  it("says a failed list read failed, with Retry, and a failed deliveries read too", async () => {
    vi.mocked(api.listWebhooks).mockRejectedValueOnce(new Error("boom"));
    render(<WebhooksSection />);
    await userEvent.click(await screen.findByRole("button", { name: "Retry" }));
    expect(await screen.findByText(HOOK.url)).toBeInTheDocument();
    vi.mocked(api.listWebhookDeliveries).mockRejectedValueOnce(new Error("boom"));
    await userEvent.click(screen.getByRole("button", { name: "Deliveries" }));
    const dialog = await screen.findByRole("dialog", { name: "Deliveries" });
    await userEvent.click(await within(dialog).findByRole("button", { name: "Retry" }));
    expect(await within(dialog).findByText("deploy_window")).toBeInTheDocument();
  });

  it("names the org's secret prefix", () => {
    expect(api.webhookSecretPrefix("acme-x")).toBe("LOOM_WEBHOOK_SECRET_ACME_2DX__");
    expect(api.webhookSecretPrefix("acme_x")).toBe("LOOM_WEBHOOK_SECRET_ACME_5FX__");
  });

  it("lists a webhook and says when the server lacks its secret", async () => {
    render(<WebhooksSection />);
    expect(await screen.findByText(HOOK.url)).toBeInTheDocument();
    expect(screen.getByText("every event", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Secret not set")).toBeInTheDocument();
  });

  it("adds a webhook for the events left checked", async () => {
    vi.mocked(api.listWebhooks).mockResolvedValueOnce({ webhooks: [] });
    render(<WebhooksSection />);
    await userEvent.click(await screen.findByRole("button", { name: "Add webhook" }));
    const dialog = await screen.findByRole("dialog", { name: "Add webhook" });
    await userEvent.type(within(dialog).getByLabelText("URL"), "https://x.example.com/h");
    await userEvent.type(within(dialog).getByLabelText("Secret variable"), "LOOM_WEBHOOK_SECRET_ACME__X");
    await userEvent.click(within(dialog).getByRole("checkbox", { name: "memory.written" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Add webhook" }));
    await waitFor(() =>
      expect(api.registerWebhook).toHaveBeenCalledWith(
        expect.objectContaining(scope),
        "https://x.example.com/h",
        "LOOM_WEBHOOK_SECRET_ACME__X",
        api.WEBHOOK_EVENTS.filter((e) => e !== "memory.written"),
      ),
    );
    await waitFor(() => expect(api.listWebhooks).toHaveBeenCalledTimes(2));
  });

  it("removes a webhook after a confirmation", async () => {
    render(<WebhooksSection />);
    await userEvent.click(await screen.findByRole("button", { name: `Actions for ${HOOK.url}` }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Remove" }));
    expect(confirmAction).toHaveBeenCalledWith(expect.objectContaining({ danger: true }));
    await waitFor(() => expect(api.removeWebhook).toHaveBeenCalledWith(expect.objectContaining(scope), "w1"));
  });

  it("removes nothing when the confirmation is declined", async () => {
    vi.mocked(confirmAction).mockResolvedValueOnce(false);
    render(<WebhooksSection />);
    await userEvent.click(await screen.findByRole("button", { name: `Actions for ${HOOK.url}` }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Remove" }));
    await waitFor(() => expect(confirmAction).toHaveBeenCalled());
    expect(api.removeWebhook).not.toHaveBeenCalled();
  });

  it("says when the webhook was already removed", async () => {
    vi.mocked(api.removeWebhook).mockResolvedValueOnce({ removed: false });
    render(<WebhooksSection />);
    await userEvent.click(await screen.findByRole("button", { name: `Actions for ${HOOK.url}` }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Remove" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("This webhook was already removed", "info"));
  });

  it("says why each delivery did not arrive, and nothing for one that did", async () => {
    const d = (event_id: string, status: "delivered" | "dead", last_status: number, last_error: string) => ({
      event_id,
      event_type: "memory.written",
      memory_key: `k-${event_id}`,
      occurred_at: "2000-01-01 00:00:00",
      status,
      attempts: 1,
      last_status,
      last_error,
    });
    vi.mocked(api.listWebhookDeliveries).mockResolvedValueOnce({
      deliveries: [d("e1", "delivered", 200, ""), d("e2", "dead", 0, "connection refused")],
    });
    render(<WebhooksSection />);
    await userEvent.click(await screen.findByRole("button", { name: "Deliveries" }));
    const dialog = await screen.findByRole("dialog", { name: "Deliveries" });
    expect(await within(dialog).findByText("connection refused")).toBeInTheDocument();
    expect(within(dialog).queryByText("HTTP 200")).toBeNull();
  });

  it("shows a refused registration in the dialog", async () => {
    vi.mocked(api.registerWebhook).mockRejectedValueOnce(new Error("secret_ref must start with the org's prefix"));
    render(<WebhooksSection />);
    await userEvent.click(await screen.findByRole("button", { name: "Add webhook" }));
    const dialog = await screen.findByRole("dialog", { name: "Add webhook" });
    await userEvent.type(within(dialog).getByLabelText("URL"), "https://x.example.com/h");
    await userEvent.type(within(dialog).getByLabelText("Secret variable"), "OTHER");
    await userEvent.click(within(dialog).getByRole("button", { name: "Add webhook" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("secret_ref must start");
  });

  it("shows a webhook's deliveries", async () => {
    render(<WebhooksSection />);
    await userEvent.click(await screen.findByRole("button", { name: "Deliveries" }));
    const dialog = await screen.findByRole("dialog", { name: "Deliveries" });
    expect(await within(dialog).findByText("deploy_window")).toBeInTheDocument();
    expect(within(dialog).getByText("dead")).toBeInTheDocument();
    expect(within(dialog).getByText("6 tries")).toBeInTheDocument();
    // The receiver's answer is why it died.
    expect(within(dialog).getByText("HTTP 500")).toBeInTheDocument();
  });
});
