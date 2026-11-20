#!/bin/sh
# Native server prerequisite installer; also used by the Docker node-builder.
set -eu

command -v node >/dev/null 2>&1 || {
  echo 'Install Node.js 22 or 24 (including npm), then rerun this script.' >&2
  exit 1
}
node -e 'const v = Number(process.versions.node.split(".")[0]); if (v < 22) process.exit(1)' || {
  echo 'The compiler indexers require Node.js 22 or newer.' >&2
  exit 1
}
command -v npm >/dev/null 2>&1 || {
  echo 'npm is required to install the compiler indexers.' >&2
  exit 1
}

toolchain_prefix=${LOOM_CODE_TOOLCHAIN_PREFIX:-"$PWD/var/code-toolchains"}
mkdir -p "$toolchain_prefix"
toolchain_prefix=$(cd "$toolchain_prefix" && pwd)
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
packages="$toolchain_prefix/lib/loom-node-tools"
mkdir -p "$packages" "$toolchain_prefix/bin"
cp "$script_dir/code-toolchains/package.json" "$script_dir/code-toolchains/package-lock.json" "$packages/"
npm ci --prefix "$packages" --ignore-scripts --no-fund --no-audit
node "$script_dir/code-toolchains/bind-native.cjs" "$packages" bun pnpm
npm rebuild --prefix "$packages" --ignore-scripts --offline --no-fund --no-audit
for tool in scip-typescript scip-python pnpm pnpx yarn yarnpkg bun bunx; do
  ln -sf "../lib/loom-node-tools/node_modules/.bin/$tool" "$toolchain_prefix/bin/$tool"
done
# Keep both Yarn lockfile formats supported without downloading a tool at
# indexing time or following a repository's executable yarnPath/plugins.
modern="$toolchain_prefix/lib/loom-yarn-modern"
mkdir -p "$modern"
cp "$script_dir/code-toolchains/yarn-modern/package.json" "$script_dir/code-toolchains/yarn-modern/package-lock.json" "$modern/"
npm ci --prefix "$modern" --ignore-scripts --no-fund --no-audit
ln -sf ../lib/loom-yarn-modern/node_modules/.bin/yarn "$toolchain_prefix/bin/yarn-modern"

# The TypeScript tier installs with this pnpm and never fetches the one a repository
# pins: it must load a workspace that sets lockfile options.
probe=$(mktemp -d)
trap 'rm -rf "$probe"' EXIT
printf 'packages: []\nlockfile:\n  includeResolutionSettings: true\n' >"$probe/pnpm-workspace.yaml"
printf '{"name":"probe","private":true}\n' >"$probe/package.json"
(cd "$probe" && "$toolchain_prefix/bin/pnpm" config list \
  --config.manage-package-manager-versions=false >/dev/null) || {
  echo 'The installed pnpm cannot load a pnpm-workspace.yaml that sets lockfile options.' >&2
  exit 1
}

printf '\nCompiler indexers installed. Set PATH in the shell that starts Loom:\n'
printf 'export PATH="%s/bin:$PATH"\n' "$toolchain_prefix"
