#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
prefix="${PREFIX:-/usr/local}"
static_dir="${STATIC_DIR:-/srv/dash-public-site}"
env_file="${ENV_FILE:-/etc/dune-public-site.env}"
user_name="${DUNE_PUBLIC_SITE_USER:-${SUDO_USER:-$USER}}"

usage() {
  cat <<EOF
Usage: sudo ./public-site/scripts/install-dune-public-site.sh [--no-systemd]

Installs the public Dune static site assets, renderer scripts, example config,
and optional systemd timer. Override paths with environment variables:

  STATIC_DIR=/var/www/dune
  ENV_FILE=/etc/dune-public-site.env
  PREFIX=/usr/local
  DUNE_PUBLIC_SITE_USER=dune

EOF
}

install_systemd=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    --help|-h)
      usage
      exit 0
      ;;
    --no-systemd)
      install_systemd=0
      shift
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root so files can be installed under $static_dir and $prefix." >&2
  exit 1
fi

install -d -m 0755 "$static_dir"
find "$repo_root/public-site/static" -maxdepth 1 -type f \( \
  -name '*.html' -o \
  -name '*.css' -o \
  -name '*.js' -o \
  -name '*.json' -o \
  -name '*.svg' -o \
  -name '*.webp' \
\) -print0 |
  while IFS= read -r -d '' asset; do
    case "$(basename "$asset")" in
      players.json|status.html|hagga-map.svg|deep-desert-map.svg|deep-desert-map-data.json|deep-desert-observations.json)
        continue
        ;;
    esac
    install -m 0644 "$asset" "$static_dir/$(basename "$asset")"
  done

install -d -m 0755 "$prefix/sbin"
install -d -m 0755 "$prefix/libexec/dune-public-directory"
install -m 0755 "$repo_root/public-site/scripts/render-dune-static-status.sh" "$prefix/sbin/render-dune-static-status.sh"
install -m 0755 "$repo_root/public-site/scripts/render-dune-public-snapshot.py" "$prefix/sbin/render-dune-public-snapshot.py"
install -m 0755 "$repo_root/public-site/scripts/dune-public-directory-signer.py" "$prefix/libexec/dune-public-directory-signer.py"
install -m 0644 "$repo_root/admin/public_directory.py" "$prefix/libexec/dune-public-directory/public_directory.py"
install -m 0755 "$repo_root/public-site/scripts/register-deep-desert-background.py" "$prefix/sbin/register-deep-desert-background.py"
install -m 0755 "$repo_root/public-site/scripts/build-federated-directory.py" "$prefix/sbin/build-federated-directory.py"
install -m 0755 "$repo_root/public-site/scripts/configure-federated-directory-sources.py" "$prefix/sbin/configure-federated-directory-sources.py"
install -m 0755 "$repo_root/public-site/scripts/configure-dune-public-site.sh" "$prefix/sbin/configure-dune-public-site.sh"
install -m 0755 "$repo_root/public-site/scripts/validate-dune-public-site.sh" "$prefix/sbin/validate-dune-public-site.sh"
install -m 0755 "$repo_root/public-site/scripts/check-dune-public-site-drift.sh" "$prefix/sbin/check-dune-public-site-drift.sh"

install -d -m 0755 "$static_dir/directory"
find "$repo_root/public-site/directory" -maxdepth 1 -type f \( -name '*.html' -o -name '*.css' -o -name '*.js' \) -print0 |
  while IFS= read -r -d '' asset; do
    install -m 0644 "$asset" "$static_dir/directory/$(basename "$asset")"
  done

if [[ ! -f "$env_file" ]]; then
  install -m 0644 "$repo_root/public-site/dune-public-site.env.example" "$env_file"
fi

if [[ "$install_systemd" -eq 1 ]]; then
  install -d -m 0755 /etc/systemd/system
  tmp_service="$(mktemp)"
  sed \
    -e "s#^User=.*#User=$user_name#" \
    -e "s#^EnvironmentFile=.*#EnvironmentFile=-$env_file#" \
    -e "s#^ExecStart=.*#ExecStart=$prefix/sbin/render-dune-static-status.sh#" \
    "$repo_root/public-site/systemd/render-dune-static-status.service" > "$tmp_service"
  install -m 0644 "$tmp_service" /etc/systemd/system/render-dune-static-status.service
  rm -f "$tmp_service"
  install -m 0644 "$repo_root/public-site/systemd/render-dune-static-status.timer" /etc/systemd/system/render-dune-static-status.timer
  signing_key_source="$(realpath -m "$repo_root/config/secrets/public-directory-ed25519.pem")"
  if [[ "$signing_key_source" == *[[:space:]]* ]]; then
    echo "signing key source path cannot contain whitespace: $signing_key_source" >&2
    exit 1
  fi
  tmp_signer_service="$(mktemp)"
  sed "s#__SIGNING_KEY_PATH__#$signing_key_source#" \
    "$repo_root/public-site/systemd/dune-public-directory-signer.service" > "$tmp_signer_service"
  install -m 0644 "$tmp_signer_service" /etc/systemd/system/dune-public-directory-signer.service
  rm -f "$tmp_signer_service"
  tmp_signer_socket="$(mktemp)"
  sed "s#__SOCKET_USER__#$user_name#" \
    "$repo_root/public-site/systemd/dune-public-directory-signer.socket" > "$tmp_signer_socket"
  install -m 0644 "$tmp_signer_socket" /etc/systemd/system/dune-public-directory-signer.socket
  rm -f "$tmp_signer_socket"
  systemctl daemon-reload
  systemctl enable --now dune-public-directory-signer.socket
  systemctl enable --now render-dune-static-status.timer
fi

echo "Installed public Dune static site to $static_dir"
if [[ "$install_systemd" -eq 1 ]]; then
  echo "Review $env_file, then run: systemctl restart render-dune-static-status.service"
else
  echo "Review $env_file, then run: $prefix/sbin/render-dune-static-status.sh"
fi
echo "Customize public text with: STATIC_DIR=$static_dir $prefix/sbin/configure-dune-public-site.sh"
