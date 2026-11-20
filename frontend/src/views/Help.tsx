import { type MouseEvent, useEffect, useState } from "react";
import { useStore } from "../lib/state";
import { type HelpPage, helpPage } from "../api/help";
import { Loading, PageHeader } from "../components/ui";
import { PageContainer } from "../layout/PageContainer";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect } from "../lib/hooks";
import { HelpText } from "./help/HelpText";

type State = { kind: "loading" } | { kind: "ready"; page: HelpPage } | { kind: "error"; message: string };

/** The page `?page=` names, as the build ships it; the index of the docs when none. */
function pageParam(): string {
  return new URLSearchParams(window.location.search).get("page") || "index.md";
}

/**
 * Loom's documentation, as the build that serves the dashboard ships it: where an
 * assistant answer's help citation leads, at the section it cites (the URL's anchor).
 * The page is read with `help.get` under the signed-in session; nothing here is public.
 */
export function Help() {
  const [path, setPath] = useState(pageParam);
  const [state, setState] = useState<State>({ kind: "loading" });
  const openDocsPage = useStore((s) => s.openDocsPage);
  // Every navigation (a Sources link, a link on the page) moves this on: read the address.
  const navSeq = useStore((s) => s.navSeq);
  const [arrived, setArrived] = useState(0);

  useEffect(() => {
    const onPop = () => {
      setPath(pageParam());
      setArrived((n) => n + 1);
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    setPath(pageParam());
    setArrived((n) => n + 1);
  }, [navSeq]);

  useAbortableEffect(
    async (signal, live) => {
      setState({ kind: "loading" });
      try {
        const page = await helpPage(path, signal);
        if (live()) setState({ kind: "ready", page });
      } catch (e) {
        if (live() && !(e instanceof DOMException && e.name === "AbortError"))
          setState({ kind: "error", message: humanizeError(e, { read: true }) });
      }
    },
    [path],
  );

  // Once the page is drawn, and on each arrival at it, scroll to the section the anchor names.
  useEffect(() => {
    if (state.kind !== "ready" || !window.location.hash) return;
    let id = window.location.hash.slice(1);
    try {
      id = decodeURIComponent(id);
    } catch {
      // A hand-typed anchor that is no valid escape is looked up as written.
    }
    document.getElementById(id)?.scrollIntoView();
  }, [state, arrived]);

  // A link to another page of the docs moves within this view, without a reload.
  const follow = (e: MouseEvent, href: string) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    e.preventDefault();
    const url = new URL(href, window.location.origin);
    openDocsPage(url.searchParams.get("page") ?? "index.md", url.hash.slice(1));
  };

  const title = state.kind === "ready" && state.page.found ? state.page.title : "Documentation";
  return (
    <PageContainer>
      <PageHeader title={title} />
      {state.kind === "loading" && <Loading label="Loading the page" />}
      {state.kind === "error" && (
        <p role="alert" className="text-sm text-danger">
          {state.message}
        </p>
      )}
      {state.kind === "ready" && !state.page.available && (
        <p className="text-sm text-text-muted">This build of Loom ships no documentation to read.</p>
      )}
      {state.kind === "ready" && state.page.available && !state.page.found && (
        <p className="text-sm text-text-muted">This build's documentation has no page {path}.</p>
      )}
      {state.kind === "ready" && state.page.found && (
        <article aria-label={state.page.title}>
          <HelpText path={state.page.path} text={state.page.text} outline={state.page.sections} onFollow={follow} />
          {state.page.truncated && (
            <p className="text-xs text-text-muted mt-4">The page is longer than this view reads; it is cut here.</p>
          )}
        </article>
      )}
    </PageContainer>
  );
}
