#!/bin/sh
set -eu

# These are build/development packages in the vendor server image. The native
# linker and game payload checks independently reject any runtime breakage.
packages="$(dpkg-query -W -f='${binary:Package}\n' 2>/dev/null | \
  sed 's/:[^:]*$//' | \
  grep -E '^(linux-hwe-[0-9].*-tools.*|linux-tools-.*|linux-libc-dev|libc6-dev|libicu-dev|libncurses-dev|libxml2-dev|lldb-15|llvm-15-dev)$' || true)"
if [ -n "$packages" ]; then
  # Intentional word splitting: dpkg package names contain no whitespace.
  # shellcheck disable=SC2086
  apt-get -y purge --no-auto-remove $packages
fi

# The game process does not use apt or its shared library. Remove the package
# manager last, after all upgrades and other purges are finished.
packages="$(dpkg-query -W -f='${binary:Package}\n' apt libapt-pkg6.0t64 2>/dev/null || true)"
if [ -n "$packages" ]; then
  # shellcheck disable=SC2086
  apt-get -y purge --no-auto-remove --allow-remove-essential $packages
fi
