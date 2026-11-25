import homeLocales from "../scripts/home-locales.json";

export type SiteLocale = keyof typeof homeLocales;
export const siteLocales = homeLocales;
export type LocaleAlternate = { language: SiteLocale | "x-default"; href: string };
const localeKeys = Object.keys(homeLocales) as SiteLocale[];
export const homeAlternates: LocaleAlternate[] = [
  ...localeKeys.map((language) => ({ language, href: homeLocales[language].href })),
  { language: "x-default", href: homeLocales.en.href },
];

export const enterpriseAlternates: LocaleAlternate[] = [
  ...localeKeys.flatMap((language) => {
    const locale = homeLocales[language];
    return "enterprise" in locale ? [{ language, href: locale.enterprise }] : [];
  }),
  { language: "x-default", href: homeLocales.en.enterprise },
];

export function enterpriseLink(language: SiteLocale) {
  const locale = homeLocales[language];
  return "enterprise" in locale
    ? { href: locale.enterprise, hreflang: language }
    : { href: homeLocales.en.enterprise, hreflang: "en" };
}
