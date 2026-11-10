import { describe, expect, it } from "vitest";

// How an error reaches the screen (docs/development/dashboard-design.md, errors): one
// lead-in a line, and a caller with its own words the error bare.

const SOURCES = import.meta.glob<string>(["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}"], {
  query: "?raw",
  import: "default",
  eager: true,
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
            // To the end of the line: a context may hold a ";" before the error's words.
            const end = text.indexOf("\n", m.index);
            const call = text.slice(m.index, end < 0 ? undefined : end);
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

describe("a stored error line", () => {
  // `const text = humanizeError(e)` then "Audit failed: ${text}": the same double lead-in
  // the two rules above catch inline, one step removed.
  it("is not put after a lead-in of its own", () => {
    const calls = Object.entries(SOURCES).flatMap(([path, text]) =>
      [...text.matchAll(/const (\w+) = humanizeError\(([^)]*)\)/g)]
        .filter((m) => !/bare: true/.test(m[2]) && new RegExp(`:\\s*\\$?\\{${m[1]}\\}`).test(text))
        .map((m) => `${path}:${text.slice(0, m.index).split("\n").length}`),
    );
    expect(calls).toEqual([]);
  });
});
