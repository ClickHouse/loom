// Leaving the page, in its own module so a test can stand in for it: jsdom performs no
// navigation.

/** Leave for the identity provider's sign-in marked `nonce` (`markSignInStarted`); it comes
 *  back to `/me?n=<nonce>#token=…`. */
export function goToSso(nonce: string): void {
  window.location.assign(`/oauth/sso?n=${encodeURIComponent(nonce)}`);
}

let reloading = false;

/** Load the page again from the server; once, however many changes ask for it. */
export function reloadPage(): void {
  if (reloading) return;
  reloading = true;
  window.location.reload();
}
