/// <reference types="vite/client" />

declare module "pdfjs-dist/build/pdf.worker.min.mjs?url" {
  const src: string;
  export default src;
}

declare module "mammoth/mammoth.browser" {
  export function extractRawText(input: { arrayBuffer: ArrayBuffer }): Promise<{ value: string; messages: unknown[] }>;
}

/** Boot globals the server injects into the dashboard (server/identity/app/dashboard_routes.py). */
interface Window {
  __LOOM_PROXY_AUTH?: boolean;
  __LOOM_AUTH_DISABLED?: boolean;
  __LOOM_SELF_SIGNUP?: boolean;
  __LOOM_SSO?: boolean;
  __LOOM_DEFAULT_NAMESPACE?: string;
}
