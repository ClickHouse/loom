// Leaving the page, in its own module so a test can stand in for it: jsdom performs no
// navigation.

/** Leave for the identity provider's sign-in; it comes back to `/me#token=…`. */
export function goToSso(): void {
  window.location.assign("/oauth/sso");
}

/** Load the page again from the server. */
export function reloadPage(): void {
  window.location.reload();
}
