/**
 * The Add-repository dialog keeps two questions apart: whether GitHub lets Loom FETCH the
 * source (a token only when GitHub refuses, or when the person says the repository is
 * private), and who may READ the index in Loom (open / restricted) — two different axes.
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from "../test/rtl";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../lib/errors";
import { AddRepositoryDialog, sandboxProblem } from "./Code";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, addCodeRepo: vi.fn(), remoteBranches: vi.fn(), isAuthDisabled: () => false };
});
vi.mock("../components/Toast", () => ({ toast: vi.fn() }));

import { addCodeRepo, remoteBranches } from "../api";

const added = () => ({
  org: "dev",
  namespace: "code-acme-app",
  repo: "acme/app",
  remote: "https://github.com/acme/app.git",
  provider: "github",
  branch: "master",
  join_policy: "open",
  classification: "standard",
  index_stage: "queued",
  my_role: "admin",
  remote_head: "abc",
});

function mount() {
  const onAdded = vi.fn();
  render(<AddRepositoryDialog open org="dev" onOpenChange={() => {}} onAdded={onAdded} />);
  return { onAdded };
}

async function fillRepo(repo = "acme/app") {
  fireEvent.change(screen.getByLabelText("Repository"), { target: { value: repo } });
}

const REMOTE = {
  org: "dev",
  repo: "acme/app",
  remote: "https://github.com/acme/app.git",
  provider: "github",
  warnings: [],
  default_branch: "master",
  branches: [
    { name: "master", sha: "a" },
    { name: "26.8", sha: "b" },
    { name: "26.7", sha: "c" },
    { name: "feature/x", sha: "d" },
  ],
  suggested: ["26.8"],
  indexed: [{ branch: "26.7", namespace: "code-app-26-7" }],
  cap: 5,
  total: 4,
  matched: 4,
  truncated: false,
  query: "",
};

describe("Add repository — fetching versus reading are two questions", () => {
  beforeEach(() => {
    vi.mocked(addCodeRepo).mockReset();
    vi.mocked(remoteBranches).mockReset();
    vi.mocked(remoteBranches).mockRejectedValue(new ApiError({ status: 422, code: "REMOTE_REFUSED", message: "no" }));
  });
  afterEach(cleanup);

  it("labels the two sections and never asks for a token up front", () => {
    mount();
    expect(screen.getByText("Fetching from Git")).toBeTruthy();
    expect(screen.getByText("Who can read the index in Loom")).toBeTruthy();
    expect(screen.queryByLabelText("Access token")).toBeNull();
    // the Restricted hint does not tie Loom's rule to the source's visibility
    const restrictedHint = screen.getByText(/approve each reader/);
    expect(screen.getByRole("radio", { name: "Restricted" })).toHaveAccessibleDescription(restrictedHint.textContent);
    expect(restrictedHint.textContent).not.toMatch(/public/i);
    // the fast path for someone who knows: opt into the token field
    fireEvent.click(screen.getByText("It is private — add a token now"));
    expect(screen.getByLabelText("Access token")).toBeTruthy();
  });

  it("asks for the token only after GitHub refuses, then resubmits with it", async () => {
    vi.mocked(addCodeRepo)
      .mockRejectedValueOnce(
        new ApiError({
          status: 422,
          code: "REMOTE_REFUSED",
          message: "GitHub did not show acme/app @ master with no token",
        }),
      )
      .mockResolvedValueOnce(added());
    const { onAdded } = mount();
    await fillRepo();
    fireEvent.click(screen.getByRole("button", { name: /Add and start indexing/ }));
    // first attempt: no token sent
    await waitFor(() => expect(addCodeRepo).toHaveBeenCalledTimes(1));
    expect(vi.mocked(addCodeRepo).mock.calls[0][1]).toMatchObject({ repo: "acme/app", access_token: "" });
    // the refusal becomes the token step, in place, with the sentence — not an error banner
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/remote will not show/i);
    expect(alert.textContent).toMatch(/acme\/app/);
    expect(alert.textContent).toMatch(/read access to Contents/);
    expect(alert.textContent).toMatch(/never shown again/);
    expect(screen.getByRole("button", { name: /Add with token/ })).toBeTruthy();
    // no token typed → the submit stays disabled
    expect(screen.getByRole("button", { name: /Add with token/ })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Access token"), { target: { value: "github_pat_x" } });
    fireEvent.click(screen.getByRole("button", { name: /Add with token/ }));
    await waitFor(() => expect(addCodeRepo).toHaveBeenCalledTimes(2));
    expect(vi.mocked(addCodeRepo).mock.calls[1][1]).toMatchObject({ access_token: "github_pat_x" });
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
  });

  it("keeps the reading rule independent of the token, and other refusals stay errors", async () => {
    vi.mocked(addCodeRepo).mockRejectedValueOnce(
      new ApiError({ status: 422, code: "REPO_QUOTA", message: "this org already holds 20 repository indexes" }),
    );
    mount();
    await fillRepo();
    fireEvent.click(screen.getByLabelText(/Restricted/));
    fireEvent.click(screen.getByRole("button", { name: /Add and start indexing/ }));
    await waitFor(() => expect(addCodeRepo).toHaveBeenCalledTimes(1));
    expect(vi.mocked(addCodeRepo).mock.calls[0][1]).toMatchObject({
      join_policy: "approval",
      classification: "restricted",
      access_token: "",
    });
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/20 repository indexes/);
    expect(screen.queryByLabelText("Access token")).toBeNull();
  });

  it("offers the branches GitHub shows, default pre-selected, and adds them in one call", async () => {
    vi.mocked(remoteBranches).mockResolvedValueOnce(REMOTE);
    vi.mocked(addCodeRepo).mockResolvedValueOnce({
      ...added(),
      also: [{ branch: "26.8", namespace: "code-app-26-8", remote_head: "b" }],
      skipped: [],
    });
    const { onAdded } = mount();
    await fillRepo();
    fireEvent.click(screen.getByRole("button", { name: /Find branches/ }));
    await waitFor(() => expect(remoteBranches).toHaveBeenCalledWith("dev", "acme/app", "", ""));
    const list = await screen.findByRole("list", { name: "Branches" });
    expect(list.textContent).toMatch(/master/);
    expect(list.textContent).toMatch(/26\.8/);
    // the default is pre-selected, the indexed one is disabled, the release one is marked
    expect(within(list).getByRole("button", { name: /^master/ })).toHaveAttribute("aria-pressed", "true");
    expect(within(list).getByRole("button", { name: /26\.7 · indexed/ })).toHaveAttribute("aria-disabled", "true");
    expect(within(list).getByRole("button", { name: "26.8 · release" })).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(within(list).getByRole("button", { name: /26\.8/ }));
    fireEvent.click(screen.getByRole("button", { name: /Add 2 branches/ }));
    await waitFor(() => expect(addCodeRepo).toHaveBeenCalledTimes(1));
    expect(vi.mocked(addCodeRepo).mock.calls[0][1]).toMatchObject({
      repo: "acme/app",
      branch: "master",
      branches: ["26.8"],
    });
    await waitFor(() => expect(onAdded).toHaveBeenCalled());
  });

  it("a long branch list is bounded: the search box shows, Search the remote asks GitHub with the query", async () => {
    const big = { ...REMOTE, total: 15898, matched: 15898, truncated: true };
    vi.mocked(remoteBranches)
      .mockResolvedValueOnce(big)
      .mockResolvedValueOnce({
        ...REMOTE,
        branches: [
          { name: "master", sha: "a" },
          { name: "22.3", sha: "z" },
        ],
        total: 15898,
        matched: 1,
        truncated: false,
        query: "22.3",
      });
    mount();
    await fillRepo();
    fireEvent.click(screen.getByRole("button", { name: /Find branches/ }));
    const search = await screen.findByLabelText("Search branches");
    expect(screen.getByText(/showing 4 of 15,898/)).toBeTruthy();
    fireEvent.change(search, { target: { value: "22.3" } });
    // not in the page: Search the remote asks the remote's full list
    expect(screen.queryByText(/press Enter/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Search the remote" }));
    await waitFor(() => expect(remoteBranches).toHaveBeenCalledTimes(2));
    expect(vi.mocked(remoteBranches).mock.calls[1]).toEqual(["dev", "acme/app", "", "22.3"]);
    await waitFor(() => expect(screen.getByRole("button", { name: /^22\.3/ })).toBeTruthy());
    // the pre-selected default survives the re-query
    expect(screen.getByRole("button", { name: /^master/ })).toHaveAttribute("aria-pressed", "true");
  });

  // A remote search answers one query in full (truncated=false); a new term that misses
  // that page still offers the remote's full list.
  it("after one remote search, a new term that misses offers Search the remote again", async () => {
    const big = { ...REMOTE, total: 15898, matched: 15898, truncated: true };
    vi.mocked(remoteBranches)
      .mockResolvedValueOnce(big)
      .mockResolvedValueOnce({
        ...REMOTE,
        branches: [
          { name: "master", sha: "a" },
          { name: "22.3", sha: "z" },
        ],
        total: 15898,
        matched: 1,
        truncated: false,
        query: "22.3",
      })
      .mockResolvedValueOnce({
        ...REMOTE,
        branches: [
          { name: "master", sha: "a" },
          { name: "Release-24", sha: "y" },
        ],
        total: 15898,
        matched: 1,
        truncated: false,
        query: "Release-24",
      });
    mount();
    await fillRepo();
    fireEvent.click(screen.getByRole("button", { name: /Find branches/ }));
    const search = await screen.findByLabelText("Search branches");
    fireEvent.change(search, { target: { value: "22.3" } });
    fireEvent.click(screen.getByRole("button", { name: "Search the remote" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /^22\.3/ })).toBeTruthy());
    // The page answers "22.3" in full: no button while the text is that query.
    expect(screen.queryByRole("button", { name: "Search the remote" })).toBeNull();
    fireEvent.change(search, { target: { value: "Release-24" } });
    fireEvent.click(screen.getByRole("button", { name: "Search the remote" }));
    await waitFor(() => expect(remoteBranches).toHaveBeenCalledTimes(3));
    expect(vi.mocked(remoteBranches).mock.calls[2]).toEqual(["dev", "acme/app", "", "Release-24"]);
    await waitFor(() => expect(screen.getByRole("button", { name: /^Release-24/ })).toBeTruthy());
    // The server's echo matches the text in another case: still no button.
    fireEvent.change(search, { target: { value: "release-24" } });
    expect(screen.queryByRole("button", { name: "Search the remote" })).toBeNull();
  });

  it("a refused branch listing becomes the token step, and the typed branch still works without a listing", async () => {
    mount();
    await fillRepo("acme/private");
    fireEvent.click(screen.getByRole("button", { name: /Find branches/ }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/remote will not show/i);
    expect(screen.getByLabelText("Access token")).toBeTruthy();
    // without a listing the single Branch field remains the way in
    expect(screen.getByLabelText("Branch")).toBeTruthy();
  });
});

describe("sandboxProblem", () => {
  it("is the severity of health's repo_jobs_unsandboxed, null without it", () => {
    const problem = (code: string, severity = "warn") => ({ code, severity, message: "", fix: "" });
    expect(sandboxProblem({ problems: [problem("repo_jobs_unsandboxed")] })).toBe("warn");
    expect(sandboxProblem({ problems: [problem("repo_jobs_unsandboxed", "error")] })).toBe("error");
    expect(sandboxProblem({ problems: [problem("fake_llm")] })).toBeNull();
    expect(sandboxProblem({ problems: "not a list" })).toBeNull();
    expect(sandboxProblem(null)).toBeNull();
  });
});
