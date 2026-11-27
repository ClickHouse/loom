import data from "blume:data";
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
  const index = documentationLink(locale, "");
  const firstTab = data.navigation.tabs[0];
  if (index.hreflang === locale || !firstTab) return index;

  const landing = documentationLink(locale, firstTab.href ?? firstTab.path);
  return landing.hreflang === locale ? landing : index;
}
