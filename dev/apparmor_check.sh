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

profile=$(sed -n '/^ *- apparmor=/{s/^ *- apparmor=\([A-Za-z0-9_.-]*\).*/\1/p;q}' "$compose")
if [[ -z "$profile" || "$profile" == unconfined ]]; then
  exit 0
fi

if [[ ! -d "$fs" ]]; then
  echo "!!! $compose asks for the AppArmor profile $profile, but this host has no AppArmor."
  echo "    Docker would ignore it but still unmask /proc and /sys (systempaths=unconfined), with"
  echo "    nothing in their place. Enable AppArmor, or drop the security_opt block (repository"
  echo "    jobs then run without the sandbox)."
  exit 1
fi

# grep reads the files itself: `cat | grep -q` fails under pipefail when grep
# exits early and cat takes SIGPIPE.
if ! grep -qsxF "$profile" "$fs"/policy/profiles/*/name; then
  echo "!!! AppArmor profile $profile is not loaded; $compose needs it. Load it once (persists across boots):"
  echo "    sudo install -m644 deploy/apparmor/$profile /etc/apparmor.d/$profile && sudo apparmor_parser -r -W /etc/apparmor.d/$profile"
  exit 1
fi
