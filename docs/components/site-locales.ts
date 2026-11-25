import homeLocales from "../scripts/home-locales.json";

export type SiteLocale = keyof typeof homeLocales;
export const siteLocales = homeLocales;
export const homeAlternates = [
  ...Object.entries(homeLocales).map(([language, { href }]) => ({ language, href })),
  { language: "x-default", href: homeLocales.en.href },
];
