/**
 * The SDK's name and version, single-sourced: `package.json` must carry the
 * same version (scripts/check-version.mjs and the dist tests check) and the
 * transport sends it in `User-Agent` and `X-Loom-SDK`.
 */
export const SDK_NAME = "loom-ts";
export const SDK_VERSION = "1.1.0";
export const SDK_IDENT = `${SDK_NAME}/${SDK_VERSION}`;
