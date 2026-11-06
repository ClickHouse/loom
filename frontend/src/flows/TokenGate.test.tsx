import { fireEvent, render, screen } from "../test/rtl";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SIGN_IN_FROM, SIGN_IN_PENDING } from "../lib/boot";
import { ApiError } from "../lib/errors";
import { axeViolations } from "../test/axe";
import { DOCS_QUICKSTART_URL, DOCS_TOKENS_URL, LinkTokenConfirm, TokenGate, resumeSsoOnce } from "./TokenGate";

// The gate says why it is showing, tells a stranger how to get in, and every field is
// labelled.

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    loginWithPassword: vi.fn(),
    selfSignup: vi.fn(),
    setToken: vi.fn(),
  };
});
import { loginWithPassword, selfSignup } from "../api";
vi.mock("../lib/navigation", () => ({ goToSso: vi.fn(), reloadPage: vi.fn() }));
import { goToSso } from "../lib/navigation";

describe("TokenGate", () => {
  afterEach(() => vi.clearAllMocks());

  it("tells a returning user their session expired, and says nothing on a first visit", () => {
    const { unmount } = render(<TokenGate onLogin={() => undefined} reason="expired" />);
    expect(screen.getByRole("status")).toHaveTextContent("Your session ended — sign in again.");
    unmount();
    render(<TokenGate onLogin={() => undefined} reason={null} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByText(/session expired/)).not.toBeInTheDocument();
  });

  it("labels every input on both tabs and passes axe", async () => {
    const { container } = render(<TokenGate onLogin={() => undefined} />);
    expect(screen.getByLabelText("Email or namespace")).toBeInstanceOf(HTMLInputElement);
    expect(screen.getByLabelText("Password")).toBeInstanceOf(HTMLInputElement);
    expect(screen.getByLabelText("Email or namespace")).toHaveAccessibleDescription(
      "The email you signed up with, or the name you picked at sign-up.",
    );
    expect(await axeViolations(container)).toEqual([]);

    await userEvent.click(screen.getByRole("tab", { name: /Sign up/ }));
    expect(screen.getByLabelText("Pick a name")).toBeInstanceOf(HTMLInputElement);
    expect(screen.getByLabelText("Email")).toBeInstanceOf(HTMLInputElement);
    expect(screen.getByLabelText("Password")).toBeInstanceOf(HTMLInputElement);
    expect(await axeViolations(container)).toEqual([]);
  });

  it("tells an operator how to get in, with links to the repo docs", () => {
    render(<TokenGate onLogin={() => undefined} />);
    expect(screen.getByText(/Running this yourself\?/)).toBeInTheDocument();
    expect(screen.getByText("loom mint-token …")).toBeInTheDocument();
    // `/me`, where the server serves the dashboard: the root may redirect elsewhere, and a
    // redirect carries the fragment, token and all.
    expect(screen.getByText("<server>/me#token=<jwt>")).toBeInTheDocument();
    expect(screen.getByText("LOOM_AUTH_REQUIRED=false")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Tokens and grants (opens in a new tab)" })).toHaveAttribute(
      "href",
      DOCS_TOKENS_URL,
    );
    expect(screen.getByRole("link", { name: "Quickstart (opens in a new tab)" })).toHaveAttribute(
      "href",
      DOCS_QUICKSTART_URL,
    );
    expect(DOCS_TOKENS_URL).toMatch(
      /^https:\/\/github\.com\/ClickHouse\/loom\/blob\/main\/docs\/guides\/tokens-and-grants\.md$/,
    );
  });

  it("shows a wrong password as a sentence with the request id, never as JSON", async () => {
    vi.mocked(loginWithPassword).mockRejectedValueOnce(
      new ApiError({ status: 401, code: "AUTH_INVALID", message: "Invalid email or password.", requestId: "req-42" }),
    );
    render(<TokenGate onLogin={() => undefined} />);
    await userEvent.type(screen.getByLabelText("Email or namespace"), "alice@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Sign-in rejected: Invalid email or password.");
    expect(alert).toHaveTextContent("request id req-42");
    expect(alert.textContent).not.toContain("{");
    expect(alert.textContent).not.toContain("401");
  });

  it("sends one login when Enter is pressed again while the first is in flight", async () => {
    vi.mocked(loginWithPassword).mockReturnValue(new Promise(() => undefined));
    render(<TokenGate onLogin={() => undefined} />);
    await userEvent.type(screen.getByLabelText("Email or namespace"), "alice@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "a-password");
    await userEvent.type(screen.getByLabelText("Password"), "{Enter}{Enter}");
    expect(loginWithPassword).toHaveBeenCalledTimes(1);
  });

  it("sends one self-signup when Enter is pressed again while the first is in flight", async () => {
    vi.mocked(selfSignup).mockReturnValue(new Promise(() => undefined));
    render(<TokenGate onLogin={() => undefined} />);
    await userEvent.click(screen.getByRole("tab", { name: /Sign up/ }));
    await userEvent.type(screen.getByLabelText("Pick a name"), "alice");
    await userEvent.type(screen.getByLabelText("Email"), "alice@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "a-long-password");
    await userEvent.type(screen.getByLabelText("Password"), "{Enter}{Enter}");
    expect(selfSignup).toHaveBeenCalledTimes(1);
  });

  // The auth calls skip request() (no token yet, no call budget) but not its timeout: a hung
  // server must end in a sentence, not a spinner. The mocks run the real calls over a fetch
  // that sees the bounded signal and fails the way AbortSignal.timeout fails.
  describe("under a hung server", () => {
    async function timingOutFetch() {
      const actual = await vi.importActual<typeof import("../api")>("../api");
      vi.mocked(loginWithPassword).mockImplementation(actual.loginWithPassword);
      vi.mocked(selfSignup).mockImplementation(actual.selfSignup);
      const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
        init?.signal instanceof AbortSignal
          ? Promise.reject(new DOMException("signal timed out", "TimeoutError"))
          : new Promise<Response>(() => undefined),
      );
      vi.stubGlobal("fetch", fetchMock);
      return fetchMock;
    }
    afterEach(() => vi.unstubAllGlobals());

    it("ends a sign-in in an error", async () => {
      const fetchMock = await timingOutFetch();
      render(<TokenGate onLogin={() => undefined} />);
      await userEvent.type(screen.getByLabelText("Email or namespace"), "alice@example.com");
      await userEvent.type(screen.getByLabelText("Password"), "a-password");
      await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
      expect(await screen.findByRole("alert")).toHaveTextContent("signal timed out");
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0][0]).toBe("/v1/auth/login");
    });

    it("ends a self-signup in an error", async () => {
      const fetchMock = await timingOutFetch();
      render(<TokenGate onLogin={() => undefined} />);
      await userEvent.click(screen.getByRole("tab", { name: /Sign up/ }));
      await userEvent.type(screen.getByLabelText("Pick a name"), "alice");
      await userEvent.type(screen.getByLabelText("Email"), "alice@example.com");
      await userEvent.type(screen.getByLabelText("Password"), "a-long-password");
      await userEvent.type(screen.getByLabelText("Password"), "{Enter}");
      expect(await screen.findByRole("alert")).toHaveTextContent("signal timed out");
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0][0]).toBe("/v1/auth/self-signup");
    });
  });

  describe("with single sign-on on", () => {
    afterEach(() => {
      delete window.__LOOM_SSO;
      sessionStorage.clear();
    });

    it("starts one sign-in by itself after a session expired, and no second within minutes", () => {
      expect(resumeSsoOnce(1_000_000)).toBe(true);
      expect(resumeSsoOnce(1_000_000 + 60_000)).toBe(false);
      expect(resumeSsoOnce(1_000_000 + 6 * 60_000)).toBe(true);
    });

    it("on an expired session remembers the page and leaves for the identity provider", () => {
      window.__LOOM_SSO = true;
      history.replaceState(null, "", "/knowledge?namespace=group-a-shared");
      render(<TokenGate onLogin={() => undefined} reason="expired" />);
      expect(goToSso).toHaveBeenCalledTimes(1);
      expect(JSON.parse(sessionStorage.getItem(SIGN_IN_FROM) ?? "{}")).toMatchObject({
        path: "/knowledge?namespace=group-a-shared",
      });
      // The sign-in carries the nonce this tab keeps: the token that brings it back is stored
      // without asking.
      const { nonce } = JSON.parse(sessionStorage.getItem(SIGN_IN_PENDING) ?? "{}");
      expect(nonce).toMatch(/^[A-Za-z0-9_-]{16,64}$/);
      expect(goToSso).toHaveBeenCalledWith(nonce);
      history.replaceState(null, "", "/");
    });

    it("says a sign-in link's token was refused, and waits for a click", () => {
      window.__LOOM_SSO = true;
      render(<TokenGate onLogin={() => undefined} reason="refused" />);
      expect(screen.getByRole("status")).toHaveTextContent(
        "The token this sign-in brought was refused — sign in again.",
      );
      expect(goToSso).not.toHaveBeenCalled();
    });

    it("waits for a click on a first visit", () => {
      window.__LOOM_SSO = true;
      render(<TokenGate onLogin={() => undefined} reason={null} />);
      expect(goToSso).not.toHaveBeenCalled();
    });

    it("sends the person to the identity provider, with no password field", () => {
      window.__LOOM_SSO = true;
      render(<TokenGate onLogin={() => undefined} reason={null} />);
      expect(screen.getByRole("link", { name: "Sign in with SSO" })).toHaveAttribute("href", "/oauth/sso");
      expect(screen.queryByLabelText(/password/i)).not.toBeInTheDocument();
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    });

    it("says a session expired", () => {
      window.__LOOM_SSO = true;
      resumeSsoOnce(); // one sign-in already started on its own: the button waits for a click
      render(<TokenGate onLogin={() => undefined} reason="expired" />);
      expect(screen.getByRole("status")).toHaveTextContent("Your session ended — sign in again.");
    });

    it("remembers the page the sign-in starts on, so the way back opens it", () => {
      window.__LOOM_SSO = true;
      history.replaceState(null, "", "/memories?namespace=group-a-shared");
      resumeSsoOnce();
      render(<TokenGate onLogin={() => undefined} reason="expired" />);
      const link = screen.getByRole("link", { name: "Sign in with SSO" });
      // jsdom does not navigate; the return from the provider is lib/linkToken.test.ts.
      link.addEventListener("click", (e) => e.preventDefault());
      fireEvent.click(link);
      expect(JSON.parse(sessionStorage.getItem(SIGN_IN_FROM) ?? "{}")).toMatchObject({
        path: "/memories?namespace=group-a-shared",
      });
      // From the bare root there is nothing to come back to.
      history.replaceState(null, "", "/");
      fireEvent.click(link);
      expect(sessionStorage.getItem(SIGN_IN_FROM)).toBeNull();
      // Each start is marked with a nonce of its own.
      const { nonce } = JSON.parse(sessionStorage.getItem(SIGN_IN_PENDING) ?? "{}");
      expect(goToSso).toHaveBeenLastCalledWith(nonce);
      expect(vi.mocked(goToSso).mock.calls[0][0]).not.toBe(nonce);
    });

    it("a click that opens another tab or window follows the bare link and marks nothing here", () => {
      window.__LOOM_SSO = true;
      render(<TokenGate onLogin={() => undefined} reason={null} />);
      const link = screen.getByRole("link", { name: "Sign in with SSO" });
      // Read once the click has passed the gate's own handler (on the React root), then
      // stopped: jsdom does not navigate.
      let followed = 0;
      const after = (e: Event) => {
        if (!e.defaultPrevented) followed++;
        e.preventDefault();
      };
      document.addEventListener("click", after);
      try {
        for (const init of [{ ctrlKey: true }, { metaKey: true }, { shiftKey: true }, { altKey: true }, { button: 1 }])
          fireEvent.click(link, init);
        expect(followed).toBe(5);
        expect(goToSso).not.toHaveBeenCalled();
        expect(sessionStorage.getItem(SIGN_IN_PENDING)).toBeNull();
        // A plain click is the gate's: it starts this tab's marked sign-in instead.
        fireEvent.click(link);
        expect(followed).toBe(5);
        expect(goToSso).toHaveBeenCalledTimes(1);
      } finally {
        document.removeEventListener("click", after);
      }
    });

    it("starts the sign-in by itself in the tab the session-expired card opens, and scrubs the ask", () => {
      window.__LOOM_SSO = true;
      history.replaceState(null, "", "/me?sign_in=sso");
      render(<TokenGate onLogin={() => undefined} reason={null} />);
      const { nonce } = JSON.parse(sessionStorage.getItem(SIGN_IN_PENDING) ?? "{}");
      expect(goToSso).toHaveBeenCalledWith(nonce);
      expect(window.location.pathname + window.location.search).toBe("/me");
      history.replaceState(null, "", "/");
    });

    it("shows the password gate when it is off", () => {
      window.__LOOM_SSO = false;
      render(<TokenGate onLogin={() => undefined} reason={null} />);
      expect(screen.queryByRole("link", { name: "Sign in with SSO" })).not.toBeInTheDocument();
      expect(screen.getAllByLabelText(/password/i).length).toBeGreaterThan(0);
    });
  });
});

describe("LinkTokenConfirm", () => {
  const claims = (subject: string | null, org: string | null) => ({ subject, org, namespaces: [], session: null });

  it("names both accounts and passes axe", async () => {
    const { container } = render(
      <LinkTokenConfirm
        claims={claims("mallory", "acme")}
        current={claims("alice", "acme")}
        onConfirm={() => undefined}
        onCancel={() => undefined}
      />,
    );
    expect(screen.getByRole("button", { name: "Sign in as mallory" })).toBeInTheDocument();
    expect(screen.getByText(/You are signed in as/)).toHaveTextContent("You are signed in as alice (acme).");
    expect(await axeViolations(container)).toEqual([]);
  });

  it("offers a plain Sign in for a token that names no subject", async () => {
    const { container } = render(
      <LinkTokenConfirm
        claims={claims(null, null)}
        current={null}
        onConfirm={() => undefined}
        onCancel={() => undefined}
      />,
    );
    expect(screen.getByRole("button", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.queryByText(/You are signed in as/)).not.toBeInTheDocument();
    expect(screen.getByText(/an account it does not name/)).toBeInTheDocument();
    expect(await axeViolations(container)).toEqual([]);
  });
});
