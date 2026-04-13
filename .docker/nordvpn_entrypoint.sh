#!/bin/sh
set -eu

token="${NORDVPN_TOKEN:-}"
if [ -z "$token" ]; then
  echo "NORDVPN_TOKEN is required" >&2
  exit 1
fi

if ! pgrep -x nordvpnd >/dev/null 2>&1; then
  /etc/init.d/nordvpn start >/dev/null
fi

for _ in 1 2 3 4 5; do
  if [ -S /run/nordvpn/nordvpnd.sock ]; then
    break
  fi
  sleep 1
done

nordvpn login --token "$token" >/dev/null

technology="${NORDVPN_TECHNOLOGY:-}"
if [ -n "$technology" ]; then
  nordvpn set technology "$technology" >/dev/null
fi

autoconnect="${NORDVPN_AUTOCONNECT:-off}"
if [ "$autoconnect" = "off" ]; then
  nordvpn set autoconnect off >/dev/null
else
  nordvpn set autoconnect on >/dev/null
fi

killswitch="${NORDVPN_KILLSWITCH:-on}"
if [ "$killswitch" = "on" ]; then
  nordvpn set killswitch on >/dev/null
else
  nordvpn set killswitch off >/dev/null
fi

country="${NORDVPN_COUNTRY:-}"
if [ -n "$country" ]; then
  nordvpn connect "$country" >/dev/null
else
  nordvpn connect >/dev/null
fi

exec "$@"
