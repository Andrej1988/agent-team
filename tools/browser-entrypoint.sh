#!/bin/bash
# agent-team-browser entrypoint (runs as root only for the firewall).
#
#   AT_BROWSER_MCP_FROM    CIDR(s) allowed to reach the MCP server (8931), space-separated
#   AT_BROWSER_VIEW_FROM   CIDR(s) allowed to reach the live view (6080), space-separated
#   AT_BROWSER_MCP_HOSTS   Host header(s) the MCP server accepts (e.g. "agent-team-browser-demo:8931")
#   AT_BROWSER_SCREEN      virtual screen, default 1280x900
#   AT_BROWSER_NO_SANDBOX  1 = start Chromium with --no-sandbox (if the seccomp profile forbids user namespaces)
set -euo pipefail

private4="10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 169.254.0.0/16 224.0.0.0/4 240.0.0.0/4"

firewall() {
  iptables -P INPUT DROP
  iptables -P OUTPUT DROP
  iptables -P FORWARD DROP
  iptables -A INPUT -i lo -j ACCEPT
  iptables -A OUTPUT -o lo -j ACCEPT
  iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  for c in ${AT_BROWSER_MCP_FROM:-}; do iptables -A INPUT -p tcp -s "$c" --dport 8931 -j ACCEPT; done
  for c in ${AT_BROWSER_VIEW_FROM:-}; do iptables -A INPUT -p tcp -s "$c" --dport 6080 -j ACCEPT; done
  # Out: the internet only — never a private, link-local or multicast address
  # (host ports, LAN devices, the other containers on the shared networks).
  # DNS goes to Docker's resolver on 127.0.0.11 (loopback, allowed above).
  for c in $private4; do iptables -A OUTPUT -d "$c" -j REJECT; done
  iptables -A OUTPUT -m conntrack --ctstate NEW -j ACCEPT
  if command -v ip6tables >/dev/null && ip6tables -L >/dev/null 2>&1; then
    ip6tables -P INPUT DROP; ip6tables -P OUTPUT DROP; ip6tables -P FORWARD DROP
    ip6tables -A INPUT -i lo -j ACCEPT; ip6tables -A OUTPUT -o lo -j ACCEPT
  fi
}

if [ "$(id -u)" = 0 ]; then
  firewall
  chown 1000:1000 /profile
  # Drop root and every capability (NET_ADMIN included) for good.
  exec setpriv --reuid=1000 --regid=1000 --init-groups --inh-caps=-all --bounding-set=-all \
    env HOME=/home/node "$0" "$@"
fi

screen="${AT_BROWSER_SCREEN:-1280x900}"
rm -f /tmp/.X99-lock /profile/SingletonLock /profile/SingletonSocket /profile/SingletonCookie
Xvfb :99 -screen 0 "${screen}x24" -nolisten tcp &
sleep 1
x11vnc -display :99 -localhost -nopw -forever -shared -quiet -rfbport 5900 &
websockify --web /usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 &

flags=(--user-data-dir=/profile --remote-debugging-port=9222 --remote-debugging-address=127.0.0.1
       --no-first-run --no-default-browser-check --disable-dev-shm-usage --password-store=basic
       --lang=de-DE --window-position=0,0 "--window-size=${screen/x/,}" --start-maximized)
[ "${AT_BROWSER_NO_SANDBOX:-0}" = 1 ] && flags+=(--no-sandbox)
# Chromium closed in the live view = start it again (same profile).
( while true; do chromium "${flags[@]}" about:blank >/tmp/chromium.log 2>&1 || true; sleep 2; done ) &

for _ in $(seq 30); do
  node -e "fetch('http://127.0.0.1:9222/json/version').then(r=>process.exit(r.ok?0:1),()=>process.exit(1))" && break
  sleep 1
done

mcp_hosts="${AT_BROWSER_MCP_HOSTS:-localhost:8931}"
npx --no-install @playwright/mcp --host 0.0.0.0 --port 8931 --allowed-hosts "$mcp_hosts" \
  --cdp-endpoint http://127.0.0.1:9222 --output-dir /tmp/mcp-out &

wait -n
echo "a browser process exited — restarting the container" >&2
exit 1
