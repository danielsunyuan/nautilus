#!/bin/sh
set -eu

PRECHECK_ARGS=""

if [ "${POLYMARKET_REQUIRE_UNBLOCKED:-1}" = "1" ]; then
  PRECHECK_ARGS="${PRECHECK_ARGS} --require-unblocked"
fi

if [ -n "${POLYMARKET_EXPECT_COUNTRY:-}" ]; then
  PRECHECK_ARGS="${PRECHECK_ARGS} --expected-country ${POLYMARKET_EXPECT_COUNTRY}"
fi

python -m scripts.polymarket_geoblock_check ${PRECHECK_ARGS}

exec "$@"
