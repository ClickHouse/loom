import { cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import type { RunRow } from "../api";
import { toast } from "../components/Toast";
import { RunGrade } from "./RunGrade";

vi.mock("../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, gradeRun: vi.fn(async () => ({ graded: 1, skipped_active: 0, unchanged: 0 })) };
});

const identity = { org: "acme", namespace: "team" };
const run = (over: Partial<RunRow> = {}) =>
  ({ run_id: "r1", grade: "", grade_source: "", grade_reason: "", graded_at: "", ...over }) as RunRow;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("RunGrade", () => {
  it("grades a closed run with the reason given and shows the verdict", async () => {
    render(<RunGrade identity={identity} run={run()} />);
    await userEvent.type(screen.getByLabelText("Why (optional)"), "CI went green");
    await userEvent.click(screen.getByRole("button", { name: "Held up" }));
    await waitFor(() => expect(api.gradeRun).toHaveBeenCalledWith(identity, "r1", "pass", "CI went green"));
    expect(await screen.findByText("Graded as held up by dashboard: CI went green")).toBeInTheDocument();
  });

  it("shows a run's existing verdict and offers a regrade", () => {
    render(<RunGrade identity={identity} run={run({ grade: "fail", grade_source: "ci", grade_reason: "red" })} />);
    expect(screen.getByText("Graded as did not hold by ci: red")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Regrade: held up" })).toBeInTheDocument();
  });

  it("says a run still active was not graded, and a verdict it already had was not changed", async () => {
    vi.mocked(api.gradeRun)
      .mockResolvedValueOnce({ graded: 0, skipped_active: 1, unchanged: 0 })
      .mockResolvedValueOnce({ graded: 0, skipped_active: 0, unchanged: 1 });
    render(<RunGrade identity={identity} run={run()} />);
    await userEvent.click(screen.getByRole("button", { name: "Held up" }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith("The run is still active; it can be graded once it ends.", "info"),
    );
    await userEvent.click(screen.getByRole("button", { name: "Held up" }));
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Already graded as held up", "info"));
    expect(screen.queryByText(/Graded as/)).toBeNull();
  });
});
