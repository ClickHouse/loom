# Dependency patches

`stream-json@1.9.1.patch` backports the assembler's `__proto__` handling from
[the upstream fix](https://github.com/uhop/stream-json/commit/2f2d35bbb547306991ded6487a279154d865a358)
for [prototype injection](https://github.com/advisories/GHSA-mjw6-4jj6-33hc).
Both ordinary and reviver writes create an own data property instead of invoking
the inherited setter.

The toolchain imports the CommonJS `stream-json/Assembler` entry point. The patch
retains that API; the upstream 3.6 release uses ESM and a different assembler API.
The manifest and lockfile bind the patch, and the dependency security tests load
the parser through its actual toolchain consumer. Remove the patch when that
consumer supports an upstream fixed version.
