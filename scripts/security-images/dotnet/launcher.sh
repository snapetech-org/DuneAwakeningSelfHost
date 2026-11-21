#!/bin/sh
set -eu
: "${DUNE_DOTNET_APP_DLL:?DUNE_DOTNET_APP_DLL is required}"
exec /usr/share/dotnet/dotnet "$DUNE_DOTNET_APP_DLL" "$@"
