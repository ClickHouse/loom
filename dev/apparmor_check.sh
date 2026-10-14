#!/usr/bin/env bash
# Can this host start the containers of a compose file that names an AppArmor
# profile? Docker refuses a profile the kernel has not loaded, and ignores the
# option on a host without AppArmor, which would leave the colours'
# `systempaths=unconfined` with nothing guarding the /proc and /sys files it
# unmasks. redeploy.sh runs this before it builds a colour.
#
# Usage: dev/apparmor_check.sh <compose file>
# Exit:  0 go (loaded, or the file names no profile) · 1 stop
#
# Override (tests): LOOM_APPARMOR_FS, default /sys/kernel/security/apparmor.
# Profile names are readable without root under policy/profiles/*/name.

set -euo pipefail

compose=${1:?usage: dev/apparmor_check.sh <compose file>}
fs=${LOOM_APPARMOR_FS:-/sys/kernel/security/apparmor}

# Every profile the file names, in any of Compose's spellings (quoted or not,
# `apparmor=` or `apparmor:`, block or flow lists), comments dropped first.
# A name that is not a plain profile name (`${PROFILE}`, empty) stops the
# check rather than passing it. BSD- and BusyBox-portable sed and grep: this
# runs on macOS too.
code=$(sed 's/#.*//' "$compose")
options=$(grep -oE "apparmor[=:][^]\"',[:space:]]*" <<<"$code" || true)
if [[ -z "$options" ]]; then
  exit 0
fi
profiles=""
while IFS= read -r option; do
  name=${option#apparmor?}
  if [[ ! "$name" =~ ^[A-Za-z0-9_.-]+$ ]]; then
    echo "!!! cannot read the AppArmor profile '$name' from $compose; refusing rather than guessing."
    exit 1
  fi
  if [[ "$name" != unconfined && " $profiles" != *" $name "* ]]; then
    profiles+="$name "
  fi
done <<<"$options"
if [[ -z "$profiles" ]]; then
  exit 0
fi

if [[ ! -d "$fs" ]]; then
  echo "!!! $compose asks for the AppArmor profile ${profiles% }, but this host has no AppArmor."
  echo "    Docker would ignore it but still unmask /proc and /sys (systempaths=unconfined), with"
  echo "    nothing in their place. Enable AppArmor, or drop the security_opt block (repository"
  echo "    jobs then run without the sandbox)."
  exit 1
fi

# grep reads the files itself: `cat | grep -q` fails under pipefail when grep
# exits early and cat takes SIGPIPE.
for profile in $profiles; do
  if ! grep -qsxF "$profile" "$fs"/policy/profiles/*/name; then
    echo "!!! AppArmor profile $profile is not loaded; $compose needs it. Load it once (persists across boots):"
    echo "    sudo install -m644 deploy/apparmor/$profile /etc/apparmor.d/$profile && sudo apparmor_parser -r -W /etc/apparmor.d/$profile"
    exit 1
  fi
done
