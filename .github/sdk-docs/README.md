# SDK documentation tooling

The API documentation command installs this project's locked tools with the
repository's locked package manager. The patch applies the directory-traversal
change from [the upstream matcher update](https://github.com/TypeStrong/typedoc/pull/3151).
The public partial-match API preserves traversal behavior with the patched
matcher, including negated patterns.

Run `npm --prefix sdk/typescript run docs` to build the SDK reference. CI runs
the same command. Remove the patch when a compatible upstream release includes
the matcher update.
