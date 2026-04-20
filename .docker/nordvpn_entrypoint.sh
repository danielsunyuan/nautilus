#!/bin/sh
set -eu

log() {
  printf '%s\n' "$*" >&2
}

login_with_token() {
  nordvpn set analytics off >/dev/null 2>&1 || true

  i=0
  while [ "$i" -lt 5 ]; do
    if timeout 30 nordvpn login --token "$NORDVPN_TOKEN"; then
      return 0
    fi
    i=$((i + 1))
    sleep 2
  done
  return 1
}

require_logged_in() {
  nordvpn account >/dev/null 2>&1
}

geoblock_ok() {
  payload="$(curl -fsSL --max-time 15 https://polymarket.com/api/geoblock 2>/dev/null || true)"
  [ -n "$payload" ] || return 1

  blocked="$(printf '%s' "$payload" | sed -n 's/.*"blocked":\([^,}]*\).*/\1/p' | tr -d '[:space:]')"
  country="$(printf '%s' "$payload" | sed -n 's/.*"country":"\([^"]*\)".*/\1/p' | tr '[:lower:]' '[:upper:]')"
  expected_country="$(printf '%s' "${NORDVPN_COUNTRY:-}" | tr '[:lower:]' '[:upper:]')"

  [ "$blocked" = "false" ] || return 1
  [ -z "$expected_country" ] || [ "$country" = "$expected_country" ] || return 1
  return 0
}

wait_for_daemon() {
  i=0
  while [ "$i" -lt 90 ]; do
    if command -v nordvpnd >/dev/null 2>&1 && ! pgrep -x nordvpnd >/dev/null 2>&1; then
      nordvpnd >/tmp/nordvpnd.log 2>&1 &
    fi
    if nordvpn status >/dev/null 2>&1; then
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done

  log "FAIL: NordVPN daemon did not become ready"
  if [ -f /tmp/nordvpnd.log ]; then
    log "nordvpnd log tail:"
    tail -n 40 /tmp/nordvpnd.log >&2 || true
  fi
  return 1
}

connect_target() {
  if [ -n "${NORDVPN_SERVER:-}" ]; then
    printf '%s' "${NORDVPN_SERVER}"
    return 0
  fi

  if [ -n "${NORDVPN_COUNTRY:-}" ]; then
    printf '%s' "${NORDVPN_COUNTRY}"
    return 0
  fi

  return 1
}

NORDVPN_TOKEN="${NORDVPN_TOKEN:-}"
if [ -z "$NORDVPN_TOKEN" ]; then
  log "FAIL: NORDVPN_TOKEN is required"
  exit 2
fi

wait_for_daemon

if ! require_logged_in; then
  login_with_token || {
    log "FAIL: NordVPN token login failed"
    exit 1
  }
fi

if ! require_logged_in; then
  log "FAIL: NordVPN account state is still logged out after token login"
  exit 1
fi

nordvpn set technology "${NORDVPN_TECHNOLOGY:-nordlynx}"

if [ -n "${NORDVPN_AUTOCONNECT:-}" ]; then
  case "${NORDVPN_AUTOCONNECT}" in
    on|ON|1|true|TRUE|yes|YES)
      nordvpn set autoconnect on
      ;;
    off|OFF|0|false|FALSE|no|NO)
      nordvpn set autoconnect off
      ;;
    *)
      log "FAIL: NORDVPN_AUTOCONNECT must be on or off"
      exit 2
      ;;
  esac
fi

target=""
if target="$(connect_target)"; then
  log "Connecting NordVPN using target: $target"
  nordvpn connect "$target"
else
  log "Connecting NordVPN using the default recommended server"
  nordvpn connect
fi

if [ "$#" -eq 0 ]; then
  set -- sleep infinity
fi

for i in $(seq 1 90); do
  if nordvpn status 2>/dev/null | grep -q 'Status: *Connected' && geoblock_ok; then
    case "${NORDVPN_KILLSWITCH:-on}" in
      on|ON|1|true|TRUE|yes|YES)
        nordvpn set killswitch on
        ;;
      off|OFF|0|false|FALSE|no|NO)
        nordvpn set killswitch off
        ;;
      *)
        log "FAIL: NORDVPN_KILLSWITCH must be on or off"
        exit 2
        ;;
    esac
    # NordVPN overwrites /etc/resolv.conf with its own DNS servers, which breaks
    # Docker's embedded DNS resolver (127.0.0.11).  Prepend it back so that
    # containers sharing this network namespace can resolve other Docker service
    # names (e.g. nautilus-redis, postgres) alongside VPN-routed lookups.
    if ! grep -qF "127.0.0.11" /etc/resolv.conf 2>/dev/null; then
      { echo "nameserver 127.0.0.11"; cat /etc/resolv.conf; } > /tmp/resolv.conf
      cp /tmp/resolv.conf /etc/resolv.conf
      log "Restored Docker DNS (127.0.0.11) to /etc/resolv.conf"
    fi
    log "NordVPN connected"
    exec "$@"
  fi
  sleep 1
done

log "FAIL: NordVPN did not reach an acceptable connected + geoblock-ok state"
exit 1
