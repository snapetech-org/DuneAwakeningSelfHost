#!/usr/bin/env bash
set -euo pipefail

env_file="${1:-.env}"

read_env() {
    local key="$1" value
    value="$(awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; found=1} END {if (!found) exit 0}' "$env_file" 2>/dev/null | tail -1)"
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    printf '%s' "$value"
}

default_iface="$(ip route show default 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="dev") {print $(i+1); exit}}')"
PUBLIC_IP="${DUNE_FAILOVER_PUBLIC_IP:-${DUNE_PUBLIC_IP:-${EXTERNAL_ADDRESS:-$(read_env EXTERNAL_ADDRESS)}}}"
if [[ -z "$PUBLIC_IP" ]]; then
    echo "DUNE_PUBLIC_IP or EXTERNAL_ADDRESS is required" >&2
    exit 1
fi
LAN_IFACE="${DUNE_LAN_IFACE:-$default_iface}"
LAN_IFACE="${LAN_IFACE:-enp17s0}"
DUNE_BRIDGE_CIDR="${DUNE_BRIDGE_CIDR:-172.31.240.0/24}"
DUNE_DOCKER_NETWORK="${DUNE_DOCKER_NETWORK:-dune_server_default}"
DUNE_DOCKER_BRIDGE_NAME="${DUNE_DOCKER_BRIDGE_NAME:-$(read_env DUNE_DOCKER_BRIDGE_NAME)}"
GAME_RMQ_PUBLIC_PORT="${GAME_RMQ_PUBLIC_PORT:-$(read_env GAME_RMQ_PUBLIC_PORT)}"
GAME_RMQ_PUBLIC_PORT="${GAME_RMQ_PUBLIC_PORT:-31982}"
GAME_UDP_PORT_RANGE="${GAME_UDP_PORT_RANGE:-$(read_env GAME_UDP_PORT_RANGE)}"
GAME_UDP_PORT_RANGE="${GAME_UDP_PORT_RANGE:-7777:7871}"
IGW_UDP_PORT_RANGE="${IGW_UDP_PORT_RANGE:-$(read_env IGW_UDP_PORT_RANGE)}"
IGW_UDP_PORT_RANGE="${IGW_UDP_PORT_RANGE:-7888:7982}"
KNOCK_DUNE_TCP_COMMENT="knock-scanner dune tcp auto-block"
DOCKER_PUBLIC_UDP_HAIRPIN_COMMENT="dune docker public udp hairpin"
DOCKER_PUBLIC_UDP_HAIRPIN_ENABLED="${DUNE_DOCKER_PUBLIC_UDP_HAIRPIN_ENABLED:-false}"

port_in_range() {
    local port="$1" range="$2" start end
    start="${range%%:*}"
    end="${range##*:}"
    [[ "$start" =~ ^[0-9]+$ && "$end" =~ ^[0-9]+$ && "$port" =~ ^[0-9]+$ ]] || return 1
    ((port >= start && port <= end))
}

has_net_admin_capability() {
    local cap_eff
    cap_eff="$(awk '$1 == "CapEff:" {print $2; exit}' /proc/self/status 2>/dev/null || true)"
    [[ "$cap_eff" =~ ^[[:xdigit:]]+$ ]] || return 1
    (( (16#$cap_eff & 4096) != 0 ))
}

if [[ "$(id -u)" -ne 0 ]] && ! has_net_admin_capability; then
    if command -v sudo >/dev/null 2>&1; then
        exec sudo \
            DUNE_PUBLIC_IP="$PUBLIC_IP" \
            DUNE_LAN_IFACE="$LAN_IFACE" \
            DUNE_BRIDGE_CIDR="$DUNE_BRIDGE_CIDR" \
            DUNE_DOCKER_NETWORK="$DUNE_DOCKER_NETWORK" \
            DUNE_DOCKER_BRIDGE_NAME="$DUNE_DOCKER_BRIDGE_NAME" \
            GAME_RMQ_PUBLIC_PORT="$GAME_RMQ_PUBLIC_PORT" \
            GAME_UDP_PORT_RANGE="$GAME_UDP_PORT_RANGE" \
            IGW_UDP_PORT_RANGE="$IGW_UDP_PORT_RANGE" \
            DUNE_DOCKER_PUBLIC_UDP_HAIRPIN_ENABLED="$DOCKER_PUBLIC_UDP_HAIRPIN_ENABLED" \
            "$0" "$env_file"
    fi
    echo "root privileges required for ip, sysctl, and iptables changes" >&2
    exit 1
fi

if ! ip link show "$LAN_IFACE" >/dev/null 2>&1; then
    echo "LAN interface not found: $LAN_IFACE" >&2
    exit 1
fi

# Let the Linux host own the public /32 on LAN as well. If LAN clients route this
# exact public IP to the Dune host, Docker's existing published-port DNAT rules handle
# the same 7777/7888/77xx/78xx UDP ports as public clients.
ip addr replace "${PUBLIC_IP}/32" dev "$LAN_IFACE" label "${LAN_IFACE}:dune"

# Keep asymmetric route checks from rejecting same-LAN public-IP reflection.
sysctl -w "net.ipv4.conf.${LAN_IFACE}.rp_filter=0" >/dev/null
sysctl -w "net.ipv4.conf.all.rp_filter=0" >/dev/null

# Ensure reflected replies from containers are masqueraded back through the LAN interface.
if ! iptables -t nat -C POSTROUTING -s "$DUNE_BRIDGE_CIDR" -o "$LAN_IFACE" -j MASQUERADE 2>/dev/null; then
    iptables -t nat -I POSTROUTING 1 -s "$DUNE_BRIDGE_CIDR" -o "$LAN_IFACE" -j MASQUERADE
fi

# Docker's raw table can block bridged container traffic before FORWARD accepts
# it, which breaks control-plane calls between game-rmq and auth/text services.
if ! iptables -t raw -C PREROUTING -s "$DUNE_BRIDGE_CIDR" -d "$DUNE_BRIDGE_CIDR" -m comment --comment "dune allow same-bridge raw control-plane" -j ACCEPT 2>/dev/null; then
    iptables -t raw -I PREROUTING 1 -s "$DUNE_BRIDGE_CIDR" -d "$DUNE_BRIDGE_CIDR" -m comment --comment "dune allow same-bridge raw control-plane" -j ACCEPT
fi

# Keep same-network container traffic working when the host FORWARD policy is
# DROP. RabbitMQ auth depends on game-rmq reaching rmq-auth-shim/text-router.
bridge_if=""
if command -v docker >/dev/null 2>&1; then
    network_id="$(docker network inspect "$DUNE_DOCKER_NETWORK" --format '{{.Id}}' 2>/dev/null || true)"
    if [ -n "$network_id" ]; then
        # Compose pins this option to dune-br0. Fall back to Docker's historical
        # br-<network-id> convention while upgrading an existing deployment.
        bridge_if="$(docker network inspect "$DUNE_DOCKER_NETWORK" \
            --format '{{index .Options "com.docker.network.bridge.name"}}' 2>/dev/null || true)"
        bridge_if="${bridge_if:-$DUNE_DOCKER_BRIDGE_NAME}"
        bridge_if="${bridge_if:-br-${network_id:0:12}}"
        if ip link show "$bridge_if" >/dev/null 2>&1 \
            && ! iptables -C FORWARD -i "$bridge_if" -o "$bridge_if" -j ACCEPT 2>/dev/null; then
            iptables -I FORWARD 1 -i "$bridge_if" -o "$bridge_if" -j ACCEPT
        fi
    fi
fi

# The host has a separate nftables inet filter forward chain (in addition to
# Docker's iptables FORWARD). When that chain has policy drop and no explicit
# accept for the Dune docker bridge, every external SYN to a published Dune
# port is silently dropped after DNAT — counters in `iptables -t nat DOCKER`
# tick up but `iptables -L FORWARD -v` shows zero packets reaching DOCKER-USER.
# Add the accept rules at runtime; persistence is left to the host's
# /etc/nftables.conf (see docs/cutover-runbook.md).
if [ -n "$bridge_if" ] && command -v nft >/dev/null 2>&1 \
    && nft list chain inet filter forward >/dev/null 2>&1; then
    if ! nft list chain inet filter forward 2>/dev/null | grep -qE "ip daddr ${DUNE_BRIDGE_CIDR//./\\.} +oifname \"${bridge_if}\""; then
        nft add rule inet filter forward ip daddr "$DUNE_BRIDGE_CIDR" oifname "$bridge_if" accept comment \"Dune docker DNAT inbound\"
    fi
    if ! nft list chain inet filter forward 2>/dev/null | grep -qE "iifname \"${bridge_if}\" +ip saddr ${DUNE_BRIDGE_CIDR//./\\.}"; then
        nft add rule inet filter forward iifname "$bridge_if" ip saddr "$DUNE_BRIDGE_CIDR" accept comment \"Dune docker bridge outbound\"
    fi
fi

# Docker's published-port DNAT intentionally excludes packets arriving from the
# same Docker bridge (`! -i br-...`). Do not enable bridge-to-public UDP hairpin
# by default: Dune S2S should use private IGW addresses, and public-IP hairpin
# can create duplicate S2S paths that destabilize Deep Desert farm membership.
if [ -n "$bridge_if" ] && command -v docker >/dev/null 2>&1 \
    && [[ "$DOCKER_PUBLIC_UDP_HAIRPIN_ENABLED" =~ ^([Tt][Rr][Uu][Ee]|1|[Yy][Ee][Ss]|[Oo][Nn])$ ]]; then
    while read -r container_id; do
        [ -n "$container_id" ] || continue
        container_ip="$(docker inspect --format "{{range .NetworkSettings.Networks}}{{if eq .NetworkID \"$network_id\"}}{{.IPAddress}}{{end}}{{end}}" "$container_id" 2>/dev/null || true)"
        [ -n "$container_ip" ] || continue
        docker inspect --format '{{range $port, $bindings := .NetworkSettings.Ports}}{{if $bindings}}{{range $bindings}}{{println $port .HostPort}}{{end}}{{end}}{{end}}' "$container_id" 2>/dev/null \
            | while read -r container_port_proto host_port; do
                [ -n "$container_port_proto" ] && [ -n "$host_port" ] || continue
                proto="${container_port_proto##*/}"
                container_port="${container_port_proto%%/*}"
                [ "$proto" = "udp" ] || continue
                if port_in_range "$host_port" "$GAME_UDP_PORT_RANGE" || port_in_range "$host_port" "$IGW_UDP_PORT_RANGE"; then
                    if ! iptables -t nat -C PREROUTING -i "$bridge_if" -d "$PUBLIC_IP" -p udp --dport "$host_port" \
                        -m comment --comment "$DOCKER_PUBLIC_UDP_HAIRPIN_COMMENT" \
                        -j DNAT --to-destination "${container_ip}:${container_port}" 2>/dev/null; then
                        iptables -t nat -I PREROUTING 1 -i "$bridge_if" -d "$PUBLIC_IP" -p udp --dport "$host_port" \
                            -m comment --comment "$DOCKER_PUBLIC_UDP_HAIRPIN_COMMENT" \
                            -j DNAT --to-destination "${container_ip}:${container_port}"
                    fi
                fi
            done
    done < <(docker ps --filter "network=${DUNE_DOCKER_NETWORK}" --format '{{.ID}}')
fi

# The host knock-scanner sync can leave this as a broad Docker DROP for the
# Dune login port, even when its nftables block set is empty.
while iptables -C DOCKER-USER -p tcp --dport "$GAME_RMQ_PUBLIC_PORT" -m comment --comment "$KNOCK_DUNE_TCP_COMMENT" -j DROP 2>/dev/null; do
    iptables -D DOCKER-USER -p tcp --dport "$GAME_RMQ_PUBLIC_PORT" -m comment --comment "$KNOCK_DUNE_TCP_COMMENT" -j DROP
done

# Local clients on the Dune host itself also receive the public address from FLS.
# Redirect only this self-host address/port set; official servers are unaffected.
if iptables -t nat -A OUTPUT -p tcp -d 127.255.255.254 --dport 1 -j REDIRECT --to-ports 1 2>/dev/null; then
    iptables -t nat -D OUTPUT -p tcp -d 127.255.255.254 --dport 1 -j REDIRECT --to-ports 1 2>/dev/null || true
    if ! iptables -t nat -C OUTPUT -p tcp -d "$PUBLIC_IP" --dport "$GAME_RMQ_PUBLIC_PORT" -j REDIRECT --to-ports "$GAME_RMQ_PUBLIC_PORT" 2>/dev/null; then
        iptables -t nat -A OUTPUT -p tcp -d "$PUBLIC_IP" --dport "$GAME_RMQ_PUBLIC_PORT" -j REDIRECT --to-ports "$GAME_RMQ_PUBLIC_PORT"
    fi

    if ! iptables -t nat -C OUTPUT -p udp -d "$PUBLIC_IP" --dport "$GAME_UDP_PORT_RANGE" -j REDIRECT 2>/dev/null; then
        iptables -t nat -A OUTPUT -p udp -d "$PUBLIC_IP" --dport "$GAME_UDP_PORT_RANGE" -j REDIRECT
    fi

    if ! iptables -t nat -C OUTPUT -p udp -d "$PUBLIC_IP" --dport "$IGW_UDP_PORT_RANGE" -j REDIRECT 2>/dev/null; then
        iptables -t nat -A OUTPUT -p udp -d "$PUBLIC_IP" --dport "$IGW_UDP_PORT_RANGE" -j REDIRECT
    fi
else
    echo "WARN iptables REDIRECT is unavailable; using DNAT fallback for local self-host redirects"
    if ! iptables -t nat -C OUTPUT -p tcp -d "$PUBLIC_IP" --dport "$GAME_RMQ_PUBLIC_PORT" -j DNAT --to-destination "127.0.0.1:${GAME_RMQ_PUBLIC_PORT}" 2>/dev/null; then
        iptables -t nat -A OUTPUT -p tcp -d "$PUBLIC_IP" --dport "$GAME_RMQ_PUBLIC_PORT" -j DNAT --to-destination "127.0.0.1:${GAME_RMQ_PUBLIC_PORT}"
    fi

    if ! iptables -t nat -C OUTPUT -p udp -d "$PUBLIC_IP" --dport "$GAME_UDP_PORT_RANGE" -j DNAT --to-destination 127.0.0.1 2>/dev/null; then
        iptables -t nat -A OUTPUT -p udp -d "$PUBLIC_IP" --dport "$GAME_UDP_PORT_RANGE" -j DNAT --to-destination 127.0.0.1
    fi

    if ! iptables -t nat -C OUTPUT -p udp -d "$PUBLIC_IP" --dport "$IGW_UDP_PORT_RANGE" -j DNAT --to-destination 127.0.0.1 2>/dev/null; then
        iptables -t nat -A OUTPUT -p udp -d "$PUBLIC_IP" --dport "$IGW_UDP_PORT_RANGE" -j DNAT --to-destination 127.0.0.1
    fi
fi

echo "Dune LAN reflection host side is active for ${PUBLIC_IP} on ${LAN_IFACE}; local self-host redirects cover tcp/${GAME_RMQ_PUBLIC_PORT}, udp/${GAME_UDP_PORT_RANGE}, and udp/${IGW_UDP_PORT_RANGE}"
