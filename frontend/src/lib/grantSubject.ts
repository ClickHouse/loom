/**
 * How a grant's subject is read: whose it is, and what it is called on screen. The gate
 * matches a subject whatever its case (server/identity/store/grants.py `stored_spellings`), so
 * these do too.
 */
import type { GrantEntry } from "../api";
import { displaySubject, isServiceSubject } from "./format";

/** A grant's spellings, lowered: its subject and the others folded onto it (`also`). */
export function grantSpellings(r: Pick<GrantEntry, "subject" | "also">): string[] {
  return [r.subject, ...(r.also ?? [])].map((s) => s.toLowerCase());
}

/** Whether a grant names one of `me` (the person's spellings), in any case. */
export function namesMe(r: Pick<GrantEntry, "subject" | "also">, me: Iterable<string>): boolean {
  const mine = new Set([...me].filter(Boolean).map((s) => s.toLowerCase()));
  return grantSpellings(r).some((s) => mine.has(s));
}

/** The person's spellings: `subject` (whoami's), and every spelling of a grant among `rows`
 *  that names it, so a grant held under only one of their other spellings is theirs too. */
export function mySpellings(subject: string, rows: Iterable<Pick<GrantEntry, "subject" | "also">>): string[] {
  if (!subject) return [];
  const out = new Set([subject.toLowerCase()]);
  for (const r of rows) if (namesMe(r, [subject])) for (const s of grantSpellings(r)) out.add(s);
  return [...out];
}

/** A subject as a person reads it: a group, an agent (a service account is one), an end
 *  user by their id, the whole org, else the subject itself. */
export function subjectLabel(subject: string, org = ""): string {
  if (subject === "everyone") return org ? `Everyone in ${org}` : "Everyone";
  if (subject.startsWith("group:")) return `group ${subject.slice(6)}`;
  if (subject.startsWith("agent:")) return `agent ${subject.slice(6)}`;
  if (isServiceSubject(subject)) return displaySubject(subject);
  if (subject.startsWith("user:")) return subject.slice(5);
  return subject;
}
