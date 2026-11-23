# Dependency patches

`@n8n__backend-common@1.47.4.patch` uses the installed parser's stream factory,
assembler entry point and completion callback. The locked parser includes the
upstream prototype-injection, nesting-depth and comment-scanning fixes.

`zod@3.25.76.patch` rejects an oversized bounded array or set before parsing its
elements. Its bundled v4 API applies the same check to bounded maps. This follows
[the upstream size-bound fix](https://github.com/colinhacks/zod/commit/a0ebf97).
The patch covers the CommonJS, ESM and TypeScript source entries. Unbounded
containers retain their validation behavior, and an overwrite before a bound
still runs before that bound.

The manifest and lockfile bind both patches. The security tests resolve packages
through their toolchain consumers and exercise prototype properties, shared
references, chunk boundaries and bounded validation in both module formats.
Remove each patch when its consumer uses a compatible upstream fixed release.
