import data from "blume:data";
import { localizedDocumentationOverview } from "../lib/documentation-routes.mjs";
import type { SiteLocale } from "./site-locales";

const translatedPaths = new Set(data.routes.filter((route) => !route.fallback && !route.draft).map(({ path }) => path));

export function documentationLink(locale: SiteLocale, path: string) {
  const translated = `/${locale}${path}`;
  // A fragment points to a specific heading. Use its source page until the localized heading is known.
  return locale !== "en" && translatedPaths.has(translated)
    ? { href: `/docs${translated}`, hreflang: locale }
    : { href: `/docs${path}`, hreflang: "en" };
}

export function documentationOverview(locale: SiteLocale) {
  return localizedDocumentationOverview(locale, (path) => translatedPaths.has(path));
}
