import { describe, expect, it } from "vitest";

// A server time is read with parseChTime (docs/development/dashboard-design.md). This checks
// Date.parse only: a regex cannot tell new Date(<string>) from new Date(<number>).

const SOURCES = import.meta.glob<string>(["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}"], {
  query: "?raw",
  import: "default",
  eager: true,
});

describe("server times", () => {
  it("are never read with Date.parse", () => {
    const calls = Object.entries(SOURCES).flatMap(([path, text]) =>
      [...text.matchAll(/\bDate\.parse\(/g)].map((m) => `${path}:${text.slice(0, m.index).split("\n").length}`),
    );
    expect(Object.keys(SOURCES).length).toBeGreaterThan(50);
    expect(calls).toEqual([]);
  });
});

describe("error toasts", () => {
  // `toastError(context, e)` keeps one lead-in, the request id and the reading time. A toast
  // that words an error itself passes `bare` (its own words are the lead-in); anything else
  // reads "Pin failed: Couldn't do that: …" or loses the request id.
  it("are raised with toastError, or word the error bare inside their own sentence", () => {
    const calls = Object.entries(SOURCES)
      .filter(([path]) => !path.endsWith("components/Toast.tsx"))
      .flatMap(([path, text]) =>
        [...text.matchAll(/\btoast\(/g)]
          .filter((m) => {
            const call = text.slice(m.index, text.indexOf(";", m.index));
            return /humanizeError\(/.test(call) && !/bare: true/.test(call);
          })
          .map((m) => `${path}:${text.slice(0, m.index).split("\n").length}`),
      );
    expect(calls).toEqual([]);
  });
});

describe("inline error lines", () => {
  // A line that brings its own lead-in ("Couldn't load collections: …") words the error
  // bare, or it reads "Couldn't load collections: Not found: …".
  it("word the error bare after their own lead-in", () => {
    const calls = Object.entries(SOURCES).flatMap(([path, text]) =>
      [...text.matchAll(/[:(]\s*\$?\{humanizeError\(([^)]*)\)/g)]
        .filter((m) => !/bare: true/.test(m[1]))
        .map((m) => `${path}:${text.slice(0, m.index).split("\n").length}`),
    );
    expect(calls).toEqual([]);
  });
});
