import homeLocales from "../scripts/home-locales.json";

export type SiteLocale = keyof typeof homeLocales;
export const siteLocales = homeLocales;
export type LocaleAlternate = { language: SiteLocale | "x-default"; href: string };
const localeKeys = Object.keys(homeLocales) as SiteLocale[];
type LocalizedField = "href" | "enterprise" | "knowledgeFactory";
const localizedPaths: Record<SiteLocale, Partial<Record<LocalizedField, string>>> = homeLocales;

function localizedAlternates(field: LocalizedField): LocaleAlternate[] {
  return [
    ...localeKeys.flatMap((language) => {
      const href = localizedPaths[language][field];
      return href ? [{ language, href }] : [];
    }),
    { language: "x-default", href: homeLocales.en[field] },
  ];
}

export const homeAlternates = localizedAlternates("href");
export const enterpriseAlternates = localizedAlternates("enterprise");
export const knowledgeFactoryAlternates = localizedAlternates("knowledgeFactory");

export function enterpriseLink(language: SiteLocale) {
  const locale = homeLocales[language];
  return "enterprise" in locale
    ? { href: locale.enterprise, hreflang: language }
    : { href: homeLocales.en.enterprise, hreflang: "en" };
}
