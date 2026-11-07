/// <reference types="vite/client" />

/** Boot globals the server injects into the dashboard (server/identity/app/dashboard_routes.py). */
interface Window {
  __LOOM_PROXY_AUTH?: boolean;
  __LOOM_AUTH_DISABLED?: boolean;
  __LOOM_SELF_SIGNUP?: boolean;
  __LOOM_SSO?: boolean;
  __LOOM_DEFAULT_NAMESPACE?: string;
  /** Whether a page of Loom's own origin asked for this load (`Sec-Fetch-Site`, else the `Referer`). */
  __LOOM_OWN_ORIGIN?: boolean;
}
