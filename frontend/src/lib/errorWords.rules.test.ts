import { describe, expect, it } from "vitest";

// How an error reaches the screen (docs/development/dashboard-design.md, errors): one
// lead-in a line, and a caller with its own words the error bare.

const SOURCES = import.meta.glob<string>(["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}"], {
  query: "?raw",
  import: "default",
  eager: true,
});

/** The whole `toast(…)` call from its opening paren to its closing one, however Biome wrapped
 *  it: a context may hold ";" or a newline before the error's words. */
function callText(text: string, start: number): string {
  let depth = 0;
  for (let i = text.indexOf("(", start); i < text.length; i += 1) {
    if (text[i] === "(") depth += 1;
    else if (text[i] === ")" && --depth === 0) return text.slice(start, i + 1);
  }
  return text.slice(start);
}

describe("error toasts", () => {
  // `toastError(context, e)` keeps one lead-in, the request id and the reading time. A toast
  // that words an error itself passes `bare` (its own words are the lead-in); anything else
  // reads "Pin failed: Couldn't do that: …". MembersDialog and ThreadActions build their own
  // sentence and send it through toastErrorLine.
  it("are raised with toastError, or word the error bare inside their own sentence", () => {
    const calls = Object.entries(SOURCES)
      .filter(([path]) => !path.endsWith("components/Toast.tsx"))
      .flatMap(([path, text]) =>
        [...text.matchAll(/\btoast(?:ErrorLine)?\(/g)]
          .filter((m) => {
            const call = callText(text, m.index);
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

describe("a bare error line in a template", () => {
  // The line closes with its own full stop (and may carry a retry tail), so text after it
  // reads "…in 3 s.; 2 messages are left".
  it("ends the template", () => {
    const calls = Object.entries(SOURCES).flatMap(([path, text]) =>
      [...text.matchAll(/\$\{humanizeError\([^)]*bare: true[^)]*\)\}([^`])/g)].map(
        (m) => `${path}:${text.slice(0, m.index).split("\n").length}`,
      ),
    );
    expect(calls).toEqual([]);
  });
});

describe("a stored error line", () => {
  // `const text = humanizeError(e)` or `setError(humanizeError(e))`, then "Audit failed:
  // ${text}" or "Couldn't load: {error}": the same double lead-in the two rules above catch
  // inline, one step removed.
  it("is not put after a lead-in of its own", () => {
    const stored = (text: string) => [
      ...[...text.matchAll(/const (\w+) = humanizeError\(([^)]*)\)/g)].map((m) => ({ m, name: m[1], args: m[2] })),
      ...[...text.matchAll(/\bset(\w+)\(humanizeError\(([^)]*)\)\)/g)].map((m) => ({
        m,
        name: m[1][0].toLowerCase() + m[1].slice(1),
        args: m[2],
      })),
    ];
    // The rendering is looked for in the component that stores the line: one file may hold
    // several, each with its own `error`.
    const component = (text: string, at: number) => {
      const starts = [...text.matchAll(/^(?:export )?function /gm)].map((m) => m.index ?? 0);
      const start = starts.filter((i) => i <= at).pop() ?? 0;
      return text.slice(start, starts.find((i) => i > at) ?? text.length);
    };
    const calls = Object.entries(SOURCES).flatMap(([path, text]) =>
      stored(text)
        .filter(
          ({ m, name, args }) =>
            !/bare: true/.test(args) && new RegExp(`:\\s*\\$?\\{${name}\\}`).test(component(text, m.index ?? 0)),
        )
        .map(({ m }) => `${path}:${text.slice(0, m.index).split("\n").length}`),
    );
    expect(calls).toEqual([]);
  });
});
