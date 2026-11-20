import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "../test/rtl";
import * as api from "../api";
import userEvent from "@testing-library/user-event";
import { CollectionBudgetDialog } from "./CollectionBudgetDialog";
import { Picker, entry, read } from "../test/libraryCollections";
import { toast } from "../components/Toast";

vi.mock("../api", async (original) => ({
  ...(await original<typeof import("../api")>()),
  getNamespace: vi.fn(),
  setCollectionBudget: vi.fn(),
}));
vi.mock("../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn() }));

beforeEach(() => {
  vi.mocked(api.getNamespace).mockResolvedValue({
    namespace: "col-a",
    daily_cost_cap_usd: 2,
    daily_user_cost_cap_usd: 1,
    budget_alert_inbox: "owned",
  } as api.NamespaceInfo);
  vi.mocked(api.setCollectionBudget).mockResolvedValue({
    namespace: "col-a",
    changed: true,
    daily_cost_cap_usd: 2,
    daily_user_cost_cap_usd: 0.5,
    budget_alert_inbox: "",
  });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("reads the collection policy and saves only changed budget fields to that collection", async () => {
  const close = vi.fn();
  render(<CollectionBudgetDialog org="o" collection={entry("col-a")} onClose={close} />);
  const field = await screen.findByLabelText("Daily cap per user (USD)");
  expect(api.getNamespace).toHaveBeenCalledWith(
    { org: "o", namespace: "col-a" },
    expect.objectContaining({ signal: expect.any(AbortSignal) }),
  );
  fireEvent.change(field, { target: { value: "0.5" } });
  fireEvent.change(screen.getByLabelText("Budget warning inbox"), { target: { value: "" } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save budget" })));
  expect(api.setCollectionBudget).toHaveBeenCalledWith("o", "col-a", {
    daily_user_cost_cap_usd: 0.5,
    budget_alert_inbox: "",
  });
  expect(close).toHaveBeenCalledTimes(1);
});

it("keeps a failed save open and sends no write before its policy read succeeds", async () => {
  vi.mocked(api.getNamespace).mockRejectedValueOnce(new Error("Read unavailable"));
  const close = vi.fn();
  render(<CollectionBudgetDialog org="o" collection={entry("col-a")} onClose={close} />);
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "Save budget" })).toBeDisabled();
  expect(api.setCollectionBudget).not.toHaveBeenCalled();
  expect(close).not.toHaveBeenCalled();
});

it("retains the draft and the server refusal on a failed write", async () => {
  vi.mocked(api.setCollectionBudget).mockRejectedValueOnce(new Error("Inbox belongs elsewhere"));
  const close = vi.fn();
  render(<CollectionBudgetDialog org="o" collection={entry("col-a")} onClose={close} />);
  fireEvent.change(await screen.findByLabelText("Budget warning inbox"), { target: { value: "foreign" } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save budget" })));
  await screen.findByRole("alert");
  expect(screen.getByLabelText("Budget warning inbox")).toHaveValue("foreign");
  expect(close).not.toHaveBeenCalled();
});

it("opens budget controls from the administered Library collection's actual menu", async () => {
  const c = entry("col-a");
  const view = render(<Picker org="o" namespace={null} current={c} collections={read([c])} onPick={vi.fn()} />);
  await userEvent.click(screen.getByRole("button", { name: "Actions for col-a" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Budget…" }));
  await waitFor(() => expect(screen.getByRole("dialog", { name: "Budget · col-a" })).toBeInTheDocument());
  expect(await screen.findByLabelText("Daily cost cap (USD)")).toHaveValue(2);
  const viewer = entry("col-a", { role: "viewer" });
  view.rerender(<Picker org="o" namespace={null} current={viewer} collections={read([viewer])} onPick={vi.fn()} />);
  await waitFor(() => expect(screen.queryByRole("dialog", { name: "Budget · col-a" })).not.toBeInTheDocument());
});

it("an old save cannot close a later collection dialog or discard its draft", async () => {
  let finish!: (value: Awaited<ReturnType<typeof api.setCollectionBudget>>) => void;
  vi.mocked(api.setCollectionBudget).mockReturnValueOnce(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  const a = entry("col-a"),
    b = entry("col-b");
  const places = read([a, b]);
  const view = render(
    <Picker org="o" namespace={null} current={a} place="col-a" collections={places} onPick={vi.fn()} />,
  );
  await userEvent.click(screen.getByRole("button", { name: "Actions for col-a" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Budget…" }));
  fireEvent.change(await screen.findByLabelText("Daily cost cap (USD)"), { target: { value: "3" } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Save budget" })));
  expect(api.setCollectionBudget).toHaveBeenCalledWith("o", "col-a", { daily_cost_cap_usd: 3 });
  view.rerender(<Picker org="o" namespace={null} current={b} place="col-b" collections={places} onPick={vi.fn()} />);
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  await userEvent.click(screen.getByRole("button", { name: "Actions for col-b" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Budget…" }));
  const draft = await screen.findByLabelText("Daily cost cap (USD)");
  fireEvent.change(draft, { target: { value: "7" } });
  await act(async () =>
    finish({
      namespace: "col-a",
      changed: true,
      daily_cost_cap_usd: 3,
      daily_user_cost_cap_usd: 1,
      budget_alert_inbox: "owned",
    }),
  );
  expect(screen.getByRole("dialog", { name: "Budget · col-b" })).toBeInTheDocument();
  expect(screen.getByLabelText("Daily cost cap (USD)")).toHaveValue(7);
  expect(toast).not.toHaveBeenCalled();
});
