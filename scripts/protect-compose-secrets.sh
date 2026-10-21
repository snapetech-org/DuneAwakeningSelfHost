#!/usr/bin/env bash
set -euo pipefail
umask 077

root="${1:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}"
root="$(cd -- "$root" && pwd -P)"
uid="$(id -u)"
env_source="$root/.env"
secrets_source="$root/config/secrets"

path_is_inside_root() {
  case "$1/" in
    "$root/"*) return 0 ;;
    *) return 1 ;;
  esac
}

validate_private_file() {
  local path="$1" mode owner
  [[ -f "$path" && ! -L "$path" ]] || return 1
  mode="$(stat -c '%a' -- "$path")"
  owner="$(stat -c '%u' -- "$path")"
  [[ "$owner" == "$uid" ]] || return 1
  (( (8#$mode & 077) == 0 ))
}

validate_private_tree() {
  local path="$1" entry mode owner
  [[ -d "$path" && ! -L "$path" ]] || return 1
  while IFS= read -r -d '' entry; do
    [[ ! -L "$entry" ]] || return 1
    [[ -d "$entry" || -f "$entry" ]] || return 1
    mode="$(stat -c '%a' -- "$entry")"
    owner="$(stat -c '%u' -- "$entry")"
    [[ "$owner" == "$uid" ]] || return 1
    (( (8#$mode & 077) == 0 )) || return 1
  done < <(find "$path" -print0)
}

validate_external_link() {
  local link="$1" kind="$2" target
  target="$(realpath -e -- "$link")" || return 1
  path_is_inside_root "$target" && return 1
  if [[ "$kind" == file ]]; then
    validate_private_file "$target"
  else
    validate_private_tree "$target"
  fi
}

has_movable_source=false
[[ -e "$env_source" && ! -L "$env_source" ]] && has_movable_source=true
[[ -d "$secrets_source" && ! -L "$secrets_source" ]] && has_movable_source=true
if [[ "$has_movable_source" == false ]]; then
  if [[ -L "$env_source" ]]; then
    validate_external_link "$env_source" file || {
      echo 'Compose dotenv path is not a protected external file.' >&2
      exit 1
    }
  elif [[ -e "$env_source" ]]; then
    echo 'Compose dotenv path is not a protected external file.' >&2
    exit 1
  fi
  if [[ -L "$secrets_source" ]]; then
    validate_external_link "$secrets_source" directory || {
      echo 'Compose secret directory is not a protected external directory.' >&2
      exit 1
    }
  elif [[ -e "$secrets_source" ]]; then
    echo 'Compose secret directory is not a protected external directory.' >&2
    exit 1
  fi
  exit 0
fi

home="${HOME:?HOME is required to protect Dune Compose secrets}"
private_root="${DUNE_PRIVATE_CONFIG_DIR:-${XDG_CONFIG_HOME:-$home/.config}/dune-selfhost}"
[[ "$private_root" == /* ]] || {
  echo 'DUNE_PRIVATE_CONFIG_DIR must be an absolute path.' >&2
  exit 1
}
private_root="$(realpath -m -- "$private_root")"
path_is_inside_root "$private_root" && {
  echo 'Dune private configuration directory must be outside the Compose workspace.' >&2
  exit 1
}

if [[ -e "$private_root" || -L "$private_root" ]]; then
  [[ -d "$private_root" && ! -L "$private_root" ]] || {
    echo 'Dune private configuration path is not a real directory.' >&2
    exit 1
  }
  root_mode="$(stat -c '%a' -- "$private_root")"
  root_owner="$(stat -c '%u' -- "$private_root")"
  if [[ "$root_owner" != "$uid" ]] || (( (8#$root_mode & 077) != 0 )); then
    echo 'Dune private configuration directory has unsafe ownership or permissions.' >&2
    exit 1
  fi
else
  mkdir -p -- "$private_root"
  chmod 700 -- "$private_root"
fi
private_root="$(cd -- "$private_root" && pwd -P)"
path_is_inside_root "$private_root" && {
  echo 'Dune private configuration directory resolves inside the Compose workspace.' >&2
  exit 1
}

if [[ -e "$env_source" && ! -L "$env_source" ]]; then
  validate_private_file "$env_source" || {
    echo 'Refusing to relocate a dotenv file with unexpected type, ownership, or permissions.' >&2
    exit 1
  }
  env_target="$private_root/compose.env"
  if [[ -e "$env_target" || -L "$env_target" ]]; then
    if ! validate_private_file "$env_target" || ! cmp -s -- "$env_source" "$env_target"; then
      echo 'Refusing to overwrite an existing private Compose dotenv file.' >&2
      exit 1
    fi
  else
    env_tmp="$env_target.tmp.$$"
    cp -- "$env_source" "$env_tmp"
    chmod 600 -- "$env_tmp"
    mv -T -- "$env_tmp" "$env_target"
  fi
  relative_env="$(realpath --relative-to="$root" "$env_target")"
  env_link_tmp="$root/.env.protected.$$"
  [[ ! -e "$env_link_tmp" && ! -L "$env_link_tmp" ]] || {
    echo 'Temporary Compose dotenv link already exists.' >&2
    exit 1
  }
  ln -s -- "$relative_env" "$env_link_tmp"
  mv -Tf -- "$env_link_tmp" "$env_source"
elif [[ -L "$env_source" ]]; then
  validate_external_link "$env_source" file || {
    echo 'Compose dotenv link does not resolve to a protected file outside the workspace.' >&2
    exit 1
  }
fi

if [[ -d "$secrets_source" && ! -L "$secrets_source" ]]; then
  validate_private_tree "$secrets_source" || {
    echo 'Refusing to relocate a secret directory with unexpected entries, ownership, or permissions.' >&2
    exit 1
  }
  secrets_target="$private_root/secrets"
  [[ ! -e "$secrets_target" && ! -L "$secrets_target" ]] || {
    echo 'Refusing to overwrite an existing private secret directory.' >&2
    exit 1
  }
  secrets_link_tmp="$root/config/.secrets.protected.$$"
  [[ ! -e "$secrets_link_tmp" && ! -L "$secrets_link_tmp" ]] || {
    echo 'Temporary secret-directory link already exists.' >&2
    exit 1
  }
  relative_secrets="$(realpath --relative-to="$root/config" "$secrets_target")"
  ln -s -- "$relative_secrets" "$secrets_link_tmp"
  mv -- "$secrets_source" "$secrets_target"
  mv -T -- "$secrets_link_tmp" "$secrets_source"
elif [[ -L "$secrets_source" ]]; then
  validate_external_link "$secrets_source" directory || {
    echo 'Compose secret-directory link does not resolve to a protected directory outside the workspace.' >&2
    exit 1
  }
fi

echo 'Protected Compose dotenv and secret sources outside the mounted workspace.'
