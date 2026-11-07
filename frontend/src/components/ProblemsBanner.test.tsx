import { act, fireEvent, render, screen } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProblemsBanner } from "./ProblemsBanner";
import { invalidateShared } from "../lib/sharedReads";

// A fresh Response per call: a body stream reads once.
const health = (body: unknown) => vi.fn(async () => new Response(JSON.stringify(body)));

describe("ProblemsBanner", () => {
  beforeEach(() => {
    sessionStorage.clear();
    invalidateShared(); // the health read is memoised across askers
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders each warn/error problem with its fix, errors first, and hides info", async () => {
    vi.stubGlobal(
      "fetch",
      health({
        status: "degraded",
        problems: [
          { code: "workers_lease_absent", severity: "info", message: "no lease", fix: "Run the workers service." },
          {
            code: "fake_llm",
            severity: "warn",
            message: "The LLM is the fake echo client.",
            fix: "Set LOOM_LLM_PROVIDER=openai with OPENAI_API_KEY.",
          },
          {
            code: "embedding_dim_mismatch",
            severity: "error",
            message: "1536-dim vs LOOM_EMBEDDING_DIM=1024.",
            fix: "Set LOOM_EMBEDDING_DIM=1536.",
          },
        ],
      }),
    );
    await act(async () => {
      render(<ProblemsBanner />);
    });
    expect(screen.getByText(/Loom is degraded/)).toBeInTheDocument();
    const items = screen.getAllByRole("listitem");
    expect(items.map((li) => li.getAttribute("data-severity"))).toEqual(["error", "warn"]);
    expect(screen.getByText("Set LOOM_EMBEDDING_DIM=1536.")).toBeInTheDocument();
    expect(screen.getByText("Set LOOM_LLM_PROVIDER=openai with OPENAI_API_KEY.")).toBeInTheDocument();
    expect(screen.queryByText("no lease")).not.toBeInTheDocument();
  });

  it("heads with the deployment's status: a failing code-index tier at error is not degraded", async () => {
    vi.stubGlobal(
      "fetch",
      health({
        status: "ok",
        problems: [
          {
            code: "code_tier_failing",
            severity: "error",
            message: "1 code-index tier is failing.",
            fix: "Set the token.",
          },
        ],
      }),
    );
    await act(async () => {
      render(<ProblemsBanner />);
    });
    expect(screen.getByText(/Loom is running with warnings — 1 problem/)).toBeInTheDocument();
    expect(screen.getByRole("listitem").getAttribute("data-severity")).toBe("error");
  });

  it("renders nothing when the server reports no problems", async () => {
    vi.stubGlobal("fetch", health({ status: "ok", problems: [] }));
    await act(async () => {
      render(<ProblemsBanner />);
    });
    expect(screen.queryByTestId("problems-banner")).not.toBeInTheDocument();
  });

  it("renders nothing, and does not throw, when problems is not a list", async () => {
    vi.stubGlobal("fetch", health({ status: "degraded", problems: "x" }));
    await act(async () => {
      render(<ProblemsBanner />);
    });
    expect(screen.queryByTestId("problems-banner")).not.toBeInTheDocument();
  });

  it("drops malformed entries and keeps the well-formed ones", async () => {
    vi.stubGlobal(
      "fetch",
      health({
        problems: [
          null,
          "fake_llm",
          { code: "no_fix", severity: "error", message: "no fix sentence" },
          { code: "fake_llm", severity: "warn", message: "fake", fix: "Set LOOM_LLM_PROVIDER=openai." },
        ],
      }),
    );
    await act(async () => {
      render(<ProblemsBanner />);
    });
    expect(screen.getByText(/Loom is running with warnings — 1 problem/)).toBeInTheDocument();
    expect(screen.queryByText("no fix sentence")).not.toBeInTheDocument();
  });

  it("is dismissible for the session", async () => {
    vi.stubGlobal(
      "fetch",
      health({
        problems: [{ code: "fake_llm", severity: "warn", message: "fake", fix: "Set LOOM_LLM_PROVIDER=openai." }],
      }),
    );
    await act(async () => {
      render(<ProblemsBanner />);
    });
    fireEvent.click(screen.getByRole("button", { name: /Dismiss for this session/ }));
    expect(screen.queryByTestId("problems-banner")).not.toBeInTheDocument();
    expect(sessionStorage.getItem("loom_problems_banner_v1_dismissed")).toBe("1");
  });
});
