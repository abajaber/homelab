#!/bin/sh
# Sonarr Custom Script connect: force seriesType=anime on series add.
# Triggered by Sonarr Connect → Custom Script (OnSeriesAdd, Test).

set -eu

case "${sonarr_eventtype:-}" in
  Test)       echo "Test event — connect wiring OK"; exit 0 ;;
  SeriesAdd)  : ;;
  *)          exit 0 ;;
esac

[ -n "${sonarr_series_id:-}" ] || { echo "missing sonarr_series_id" >&2; exit 1; }

API_KEY=$(sed -n 's:.*<ApiKey>\([^<]*\)</ApiKey>.*:\1:p' /config/config.xml)
[ -n "$API_KEY" ] || { echo "could not extract ApiKey" >&2; exit 1; }

BASE="http://localhost:8989/api/v3"
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

curl -sf -H "X-Api-Key: $API_KEY" "$BASE/series/$sonarr_series_id" -o "$TMP" \
  || { echo "GET series failed" >&2; exit 1; }

# already anime? no-op
grep -q '"seriesType":"anime"' "$TMP" && { echo "[$sonarr_series_id] already anime"; exit 0; }

sed -i 's/"seriesType":"[^"]*"/"seriesType":"anime"/' "$TMP"

curl -sf -H "X-Api-Key: $API_KEY" -H "Content-Type: application/json" \
  -X PUT "$BASE/series/$sonarr_series_id" --data-binary "@$TMP" -o /dev/null \
  || { echo "PUT series failed" >&2; exit 1; }

echo "[$sonarr_series_id] flipped to seriesType=anime"
