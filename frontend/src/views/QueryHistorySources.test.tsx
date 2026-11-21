import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, onTestFinished, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "../test/rtl";
import { axeViolations } from "../test/axe";
import { addQueryHistory, listQueryHistory, removeQueryHistory, type QueryHistorySource } from "../api/queryHistory";
import { QueryHistorySources } from "./QueryHistorySources";

vi.mock("../api/queryHistory", () => ({
  addQueryHistory: vi.fn(),
  listQueryHistory: vi.fn(),
  removeQueryHistory: vi.fn(),
}));
const source: QueryHistorySource = {
  name: "metrics",
  source_id: "source",
  kind: "clickhouse_queries",
  host: "db.example",
  port: 8443,
  secure: true,
  user: "reader",
  databases: ["analytics"],
  cluster: "",
  created_at: "2026-01-01",
};
beforeEach(() => {
  vi.resetAllMocks();
});

it("connects only the entered database scope and clears the credential form", async () => {
  vi.mocked(listQueryHistory)
    .mockResolvedValueOnce({ enabled: true, sources: [] })
    .mockResolvedValue({ enabled: true, sources: [source] });
  vi.mocked(addQueryHistory).mockResolvedValue(source);
  const { container } = render(<QueryHistorySources org="o" namespace="n" />);
  fireEvent.click(screen.getByRole("button", { name: "Query history" }));
  fireEvent.click(await screen.findByRole("button", { name: "Connect query history" }));
  for (const [label, value] of [
    ["Source name", "metrics"],
    ["ClickHouse host", "db.example"],
    ["Read-only username", "reader"],
    ["Password", "test-credential"],
    ["Databases (comma separated)", "analytics, finance"],
  ]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }
  expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
  expect(await axeViolations(container)).toEqual([]);
  fireEvent.click(screen.getByRole("button", { name: /^Connect$/ }));
  await screen.findByRole("button", { name: "Actions for metrics" });
  expect(addQueryHistory).toHaveBeenCalledWith(
    { org: "o", namespace: "n" },
    {
      name: "metrics",
      host: "db.example",
      port: 8443,
      user: "reader",
      password: "test-credential",
      databases: ["analytics", "finance"],
      cluster: "",
      secure: true,
    },
  );
  expect(screen.queryByLabelText("Password")).toBeNull();
});

it("keeps disconnect available when the operator turns drafting off", async () => {
  vi.mocked(listQueryHistory)
    .mockResolvedValueOnce({ enabled: false, sources: [source] })
    .mockResolvedValue({ enabled: false, sources: [] });
  vi.mocked(removeQueryHistory).mockResolvedValue({ removed: true });
  render(<QueryHistorySources org="o" namespace="n" />);
  fireEvent.click(screen.getByRole("button", { name: "Query history" }));
  await userEvent.click(await screen.findByRole("button", { name: "Actions for metrics" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Disconnect" }));
  await waitFor(() => expect(removeQueryHistory).toHaveBeenCalledWith({ org: "o", namespace: "n" }, "metrics"));
  expect(screen.queryByRole("button", { name: "Connect query history" })).toBeNull();
  await waitFor(() => expect(screen.getByRole("button", { name: "Query history" })).toHaveFocus());
});

it("does not show the old namespace's late source response", async () => {
  let finish!: (value: { enabled: boolean; sources: QueryHistorySource[] }) => void;
  vi.mocked(listQueryHistory)
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    )
    .mockResolvedValue({ enabled: true, sources: [] });
  const { rerender } = render(<QueryHistorySources org="o" namespace="first" />);
  fireEvent.click(screen.getByRole("button", { name: "Query history" }));
  await waitFor(() => expect(listQueryHistory).toHaveBeenCalledTimes(1));
  rerender(<QueryHistorySources org="o" namespace="second" />);
  await screen.findByText("No query-history sources");
  finish({ enabled: true, sources: [source] });
  await waitFor(() => expect(screen.queryByText("metrics")).toBeNull());
});

it("delays its initial loading label for 300 ms and removes it when sources arrive", async () => {
  vi.useFakeTimers();
  onTestFinished(() => {
    vi.useRealTimers();
  });
  let finish!: (value: { enabled: boolean; sources: QueryHistorySource[] }) => void;
  vi.mocked(listQueryHistory).mockReturnValue(
    new Promise((resolve) => {
      finish = resolve;
    }),
  );
  render(<QueryHistorySources org="o" namespace="n" />);
  fireEvent.click(screen.getByRole("button", { name: "Query history" }));
  expect(screen.queryByText(/Loading query-history sources/)).toBeNull();
  act(() => vi.advanceTimersByTime(299));
  expect(screen.queryByText(/Loading query-history sources/)).toBeNull();
  act(() => vi.advanceTimersByTime(1));
  expect(screen.getByText(/Loading query-history sources/)).toBeVisible();
  await act(async () => finish({ enabled: true, sources: [source] }));
  expect(screen.queryByText(/Loading query-history sources/)).toBeNull();
  expect(screen.getByText("metrics")).toBeVisible();
});

it("never flashes a loading label for a fast source response", async () => {
  vi.useFakeTimers();
  onTestFinished(() => {
    vi.useRealTimers();
  });
  vi.mocked(listQueryHistory).mockResolvedValue({ enabled: true, sources: [source] });
  render(<QueryHistorySources org="o" namespace="n" />);
  fireEvent.click(screen.getByRole("button", { name: "Query history" }));
  expect(screen.queryByText(/Loading query-history sources/)).toBeNull();
  await act(async () => {});
  act(() => vi.advanceTimersByTime(300));
  expect(screen.queryByText(/Loading query-history sources/)).toBeNull();
  expect(screen.getByText("metrics")).toBeVisible();
});

it("keeps displayed sources busy until a refresh settles", async () => {
  let refresh!: (value: { enabled: boolean; sources: QueryHistorySource[] }) => void;
  vi.mocked(listQueryHistory)
    .mockResolvedValueOnce({ enabled: false, sources: [source] })
    .mockReturnValueOnce(
      new Promise((resolve) => {
        refresh = resolve;
      }),
    );
  vi.mocked(removeQueryHistory).mockResolvedValue({ removed: true });
  const { container } = render(<QueryHistorySources org="o" namespace="n" />);
  fireEvent.click(screen.getByRole("button", { name: "Query history" }));
  await userEvent.click(await screen.findByRole("button", { name: "Actions for metrics" }));
  await userEvent.click(await screen.findByRole("menuitem", { name: "Disconnect" }));
  await waitFor(() => expect(listQueryHistory).toHaveBeenCalledTimes(2));
  expect(screen.getByText("metrics")).toBeVisible();
  const retained = screen.getByText("metrics").closest("[aria-busy]");
  expect(retained).toHaveAttribute("aria-busy", "true");
  expect(screen.queryByText(/Loading query-history sources/)).toBeNull();
  await act(async () => refresh({ enabled: false, sources: [] }));
  expect(screen.getByText("No query-history sources")).toBeVisible();
  expect(retained).toHaveAttribute("aria-busy", "false");
  expect(await axeViolations(container)).toEqual([]);
});
