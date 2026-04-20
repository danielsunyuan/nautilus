#!/bin/sh
set -eu

if ! command -v nordvpn >/dev/null 2>&1; then
  printf '%s\n' "nordvpn CLI not available" >&2
  exit 1
fi

if ! nordvpn account >/dev/null 2>&1; then
  printf '%s\n' "NordVPN account is not logged in" >&2
  exit 1
fi

status="$(nordvpn status 2>&1 || true)"
if ! printf '%s\n' "$status" | grep -q 'Status: *Connected'; then
  printf '%s\n' "$status" >&2
  exit 1
fi

payload="$(curl -fsSL --max-time 15 https://polymarket.com/api/geoblock 2>/dev/null || true)"
if [ -z "$payload" ]; then
  printf '%s\n' "Polymarket geoblock endpoint unavailable" >&2
  exit 1
fi

blocked="$(printf '%s' "$payload" | sed -n 's/.*"blocked":\([^,}]*\).*/\1/p' | tr -d '[:space:]')"
country="$(printf '%s' "$payload" | sed -n 's/.*"country":"\([^"]*\)".*/\1/p' | tr '[:lower:]' '[:upper:]')"
expected_country="$(printf '%s' "${NORDVPN_COUNTRY:-}" | tr '[:lower:]' '[:upper:]')"

if [ "$blocked" != "false" ]; then
  printf '%s\n' "$payload" >&2
  exit 1
fi

if [ -n "$expected_country" ] && [ "$country" != "$expected_country" ]; then
  printf '%s\n' "$payload" >&2
  exit 1
fi

exit 0
