import { cleanup, render, screen, waitFor } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { RunGrade } from "./RunGrade";

vi.mock("../components/Toast", () => ({ toast: vi.fn(), toastError: vi.fn(), ToastViewport: () => null }));
vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, gradeRun: vi.fn(async () => ({ graded: 1, skipped_active: 0 })) };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("RunGrade", () => {
  it("grades a closed run with the reason given", async () => {
    render(<RunGrade identity={{ org: "acme", namespace: "team" }} runId="r1" />);
    await userEvent.type(screen.getByLabelText("Why (optional)"), "CI went green");
    await userEvent.click(screen.getByRole("button", { name: "Held up" }));
    await waitFor(() =>
      expect(api.gradeRun).toHaveBeenCalledWith({ org: "acme", namespace: "team" }, "r1", "pass", "CI went green"),
    );
    expect(await screen.findByText("Graded as held up.")).toBeInTheDocument();
  });
});
