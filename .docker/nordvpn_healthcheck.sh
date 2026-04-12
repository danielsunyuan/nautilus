#!/bin/sh
set -eu

status="$(nordvpn status 2>/dev/null || true)"

echo "$status" | grep -q "Status: Connected"
