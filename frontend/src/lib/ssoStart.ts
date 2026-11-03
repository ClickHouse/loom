/** Leave for the identity provider's sign-in; it comes back to `/me#token=…`. Its own
 *  module so a test can stand in for the navigation, which jsdom does not perform. */
export function goToSso(): void {
  window.location.assign("/oauth/sso");
}
