#!/usr/bin/env bash
# Keep bearer and short-lived change-contract headers out of curl argv.
snape_curl_config_escape() {
  local value="$1"
  if [[ "$value" == *$'\n'* || "$value" == *$'\r'* ]]; then
    printf 'refusing a newline in a sensitive HTTP header\n' >&2
    return 2
  fi
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

snape_curl() {
  local -a args=() sensitive_headers=()
  local arg header lower escaped
  while (($#)); do
    arg="$1"
    if [[ "$arg" == -H || "$arg" == --header ]]; then
      if (($# < 2)); then
        args+=("$arg")
        shift
        continue
      fi
      header="$2"
      lower="${header,,}"
      case "$lower" in
        'authorization: bearer '*|'x-dash-change-contract: '*) sensitive_headers+=("$header") ;;
        *) args+=("$arg" "$header") ;;
      esac
      shift 2
      continue
    fi
    if [[ "$arg" == --header=* || ( "$arg" == -H* && "$arg" != -H ) ]]; then
      if [[ "$arg" == --header=* ]]; then header="${arg#*=}"; else header="${arg:2}"; fi
      lower="${header,,}"
      case "$lower" in
        'authorization: bearer '*|'x-dash-change-contract: '*) sensitive_headers+=("$header") ;;
        *) args+=("$arg") ;;
      esac
      shift
      continue
    fi
    args+=("$arg")
    shift
  done

  if ((${#sensitive_headers[@]} == 0)); then
    command curl "${args[@]}"
    return
  fi

  {
    for header in "${sensitive_headers[@]}"; do
      if ! escaped="$(snape_curl_config_escape "$header")"; then
        return 2
      fi
      printf 'header = "%s"\n' "$escaped"
    done
  } | command curl --config - "${args[@]}"
}

curl() {
  snape_curl "$@"
}
