#!/usr/bin/env bash
# Run only on a fresh Ubuntu 24.04 VPN VM after reviewing this script.
set -euo pipefail
umask 077

if [[ ${EUID} -ne 0 ]]; then
    echo "Run with sudo." >&2
    exit 1
fi
port=${1:-51820}
if [[ ! $port =~ ^[0-9]{1,5}$ ]] || ((10#$port < 1024 || 10#$port > 65535)); then
    echo "Choose one UDP port between 1024 and 65535." >&2
    exit 1
fi
port=$((10#$port))
if [[ -e /etc/wireguard/wg0.conf || -e /etc/sysctl.d/90-gcp-vpn.conf ]]; then
    echo "Existing VPN setup found. Refusing to overwrite it." >&2
    exit 1
fi
source /etc/os-release
if [[ $ID != ubuntu || $VERSION_ID != 24.04 ]]; then
    echo "This script targets a fresh Ubuntu 24.04 VM." >&2
    exit 1
fi
if command -v ufw >/dev/null && ufw status | grep -q '^Status: active'; then
    echo "UFW is active. Configure forwarding rules explicitly before using this script." >&2
    exit 1
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y wireguard iptables qrencode
interface=$(ip -4 route show default | awk 'NR==1 {print $5}')
if [[ ! $interface =~ ^[a-zA-Z0-9_.:-]+$ ]]; then
    echo "Cannot determine the IPv4 uplink interface." >&2
    exit 1
fi
install -d -m 700 /etc/wireguard /etc/wireguard/clients
private_key=$(wg genkey)
cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
Address = 10.77.0.1/24
ListenPort = $port
PrivateKey = $private_key
PostUp = iptables -I FORWARD 1 -i %i -o $interface -s 10.77.0.0/24 -j ACCEPT; iptables -I FORWARD 1 -i $interface -o %i -d 10.77.0.0/24 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -A POSTROUTING -s 10.77.0.0/24 -o $interface -j MASQUERADE
PostDown = iptables -D FORWARD -i %i -o $interface -s 10.77.0.0/24 -j ACCEPT; iptables -D FORWARD -i $interface -o %i -d 10.77.0.0/24 -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT; iptables -t nat -D POSTROUTING -s 10.77.0.0/24 -o $interface -j MASQUERADE
EOF
unset private_key
chmod 600 /etc/wireguard/wg0.conf
printf 'net.ipv4.ip_forward=1\n' > /etc/sysctl.d/90-gcp-vpn.conf
sysctl --system >/dev/null
systemctl enable --now wg-quick@wg0
echo "WireGuard started on UDP $port. Next: sudo bash add-peer.sh pc 2 PUBLIC_IPV4"
echo "This setup forwards IPv4 only. Do not add IPv6 internet routes without configuring IPv6 server support."
