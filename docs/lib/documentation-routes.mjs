export const gettingStartedOverview = "/getting-started/overview";

/**
 * @param {string} locale
 * @param {(path: string) => boolean} hasPath Checks a route relative to the docs mount.
 */
export function localizedDocumentationOverview(locale, hasPath) {
  if (locale !== "en") {
    const index = `/${locale}`;
    const path = [index, `${index}${gettingStartedOverview}`].find(hasPath);
    if (path) return { href: `/docs${path}`, hreflang: locale };
  }
  return { href: "/docs", hreflang: "en" };
}
