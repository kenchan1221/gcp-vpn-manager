#!/usr/bin/env bash
# Give each device a unique name and host number; never reuse private keys.
set -euo pipefail
umask 077

if [[ ${EUID} -ne 0 || $# -ne 3 ]]; then
    echo "Usage: sudo bash add-peer.sh NAME HOST_NUMBER PUBLIC_IPV4" >&2
    exit 1
fi
name=$1
number=$2
endpoint=$3
if [[ ! $name =~ ^[a-z][a-z0-9-]{0,19}$ ]] ||
   [[ ! $number =~ ^[0-9]{1,3}$ ]] || ((10#$number < 2 || 10#$number > 254)); then
    echo "Name must be lowercase (max 20 chars); host number must be 2-254." >&2
    exit 1
fi
number=$((10#$number))
python3 - "$endpoint" <<'PY'
import ipaddress
import sys
address = ipaddress.IPv4Address(sys.argv[1])
if not address.is_global:
    raise SystemExit("Use the VM's EXTERNAL public IPv4, not its internal address.")
PY
[[ -f /etc/wireguard/wg0.conf ]] || { echo "Run setup-server.sh first." >&2; exit 1; }
systemctl is-active --quiet wg-quick@wg0 || { echo "Start wg-quick@wg0 first." >&2; exit 1; }
exec 9>/etc/wireguard/peer.lock
flock -n 9 || { echo "Another peer update is running." >&2; exit 1; }
profile="/etc/wireguard/clients/$name.conf"
address="10.77.0.$number"
if [[ -e $profile ]] || grep -Fq "AllowedIPs = $address/32" /etc/wireguard/wg0.conf; then
    echo "Device name or tunnel address already exists. Refusing to overwrite." >&2
    exit 1
fi
port=$(wg show wg0 listen-port)
server_public=$(wg show wg0 public-key)
client_private=$(wg genkey)
client_public=$(printf '%s\n' "$client_private" | wg pubkey)
preshared=$(wg genpsk)
cat > "$profile" <<EOF
[Interface]
PrivateKey = $client_private
Address = $address/32
DNS = 1.1.1.1, 1.0.0.1
MTU = 1380

[Peer]
PublicKey = $server_public
PresharedKey = $preshared
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = $endpoint:$port
PersistentKeepalive = 25
EOF
cat >> /etc/wireguard/wg0.conf <<EOF

# Device: $name
[Peer]
PublicKey = $client_public
PresharedKey = $preshared
AllowedIPs = $address/32
EOF
unset client_private preshared
chmod 600 "$profile" /etc/wireguard/wg0.conf
wg syncconf wg0 <(wg-quick strip wg0)
echo "Created $profile. Transfer it privately; do not commit it to a repository."
echo "For phone QR (contains the private key): sudo qrencode -t ansiutf8 < $profile"
echo "IPv6 is sent into the tunnel but has no internet egress in this IPv4-only setup."
