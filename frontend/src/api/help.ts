// help.get: a page of Loom's documentation as this build ships it, the same for every
// caller (server/help/api/models.py).
import { post } from "../api";

export interface HelpOutlineEntry {
  section: string;
  anchor: string;
  /** The heading as the page writes it. */
  heading: string;
}

export interface HelpPage {
  available: boolean;
  found: boolean;
  path: string;
  title: string;
  section: string;
  anchor: string;
  audience: "user" | "operator";
  text: string;
  truncated: boolean;
  next_offset: number;
  sections: HelpOutlineEntry[];
}

// A page longer than this many reads is shown cut: the docs ship with the build, so this
// bounds only a page far past any that ships.
const MAX_READS = 20;

/** The whole page: one read, then on from each cut read's `next_offset`. */
export async function helpPage(path: string, signal?: AbortSignal): Promise<HelpPage> {
  const first = await post<HelpPage>("/v1/help.get", { path }, signal);
  let page = first;
  let text = first.text;
  for (let n = 1; page.truncated && n < MAX_READS; n++) {
    page = await post<HelpPage>("/v1/help.get", { path, offset: page.next_offset }, signal);
    text += page.text;
  }
  return { ...first, text, truncated: page.truncated, next_offset: page.next_offset };
}
