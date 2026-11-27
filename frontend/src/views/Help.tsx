import { type MouseEvent, useEffect, useState } from "react";
import { Accordion } from "@clickhouse/click-ui/Accordion";
import { Link } from "@clickhouse/click-ui/Link";
import { HELP_HOME_PAGE, urlParam, useStore } from "../lib/state";
import { type HelpPage, helpPage } from "../api/help";
import { Loading, PageHeader } from "../components/ui";
import { PageContainer } from "../layout/PageContainer";
import { humanizeError } from "../lib/errorWords";
import { useAbortableEffect } from "../lib/hooks";
import { HelpText, pageHref } from "./help/HelpText";

type State = { kind: "loading" } | { kind: "ready"; page: HelpPage } | { kind: "error"; message: string };

/** The page `?page=` names, as the build ships it; the index of the docs when none. */
function pageParam(): string {
  return urlParam("page") || HELP_HOME_PAGE;
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
    openDocsPage(url.searchParams.get("page") ?? HELP_HOME_PAGE, url.hash.slice(1));
  };

  const title = state.kind === "ready" && state.page.found ? state.page.title : "Documentation";
  const home = pageHref(HELP_HOME_PAGE);
  return (
    <PageContainer>
      <PageHeader
        title={title}
        actions={
          path !== HELP_HOME_PAGE && (
            <Link href={home} onClick={(e: MouseEvent) => follow(e, home)}>
              Documentation home
            </Link>
          )
        }
      />
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
      {state.kind === "ready" && state.page.found && <HelpArticle page={state.page} onFollow={follow} />}
    </PageContainer>
  );
}

function HelpArticle({ page, onFollow }: { page: HelpPage; onFollow: (event: MouseEvent, href: string) => void }) {
  const sections = page.sections.filter(({ anchor }) => anchor).slice(1);
  const contents = !page.truncated && sections.length > 0 && (
    <nav aria-label="On this page">
      <ul className="space-y-1">
        {sections.map(({ anchor, heading }) => (
          <li key={anchor}>
            <Link href={`#${anchor}`} size="sm" className="inline-flex items-center min-h-6 pointer-coarse:min-h-11">
              {heading}
            </Link>
          </li>
        ))}
      </ul>
    </nav>
  );
  return (
    <div className={contents ? "grid items-start gap-4 lg:grid-cols-[minmax(0,74ch)_minmax(0,12rem)] lg:gap-8" : ""}>
      {contents && (
        <div className="lg:order-last lg:sticky lg:top-5">
          <div className="hidden lg:block border-l border-border pl-4">
            <h2 className="mb-2 text-sm">On this page</h2>
            {contents}
          </div>
          <Accordion className="lg:hidden" title="On this page" size="sm">
            {contents}
          </Accordion>
        </div>
      )}
      <article aria-label={page.title} className="min-w-0 max-w-[74ch]">
        <HelpText path={page.path} text={page.text} outline={page.sections} onFollow={onFollow} />
        {page.truncated && <p className="mt-4 text-xs text-text-muted">This page exceeds the reading limit.</p>}
      </article>
    </div>
  );
}
