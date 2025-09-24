#!/bin/bash

BASE_URL=${SMOKE_BASE_URL:-http://localhost:8000}

smoke_output=$(make smoke 2>&1)
smoke_status=$?

call_endpoint() {
  local path="$1"
  local label="$2"
  local url="${BASE_URL}${path}"
  local response
  response=$(curl -sS --max-time 5 "$url" 2>&1)
  local status=$?
  if [ $status -eq 0 ]; then
    printf '--- %s (%s) ---\n%s\n' "$label" "$url" "$response"
  else
    printf '--- %s (%s) FAILED ---\n%s\n' "$label" "$url" "$response"
  fi
}

cat <<HERE
--- SMOKE OUTPUT ---
$smoke_output
--- SMOKE EXIT STATUS ---
$smoke_status
HERE

call_endpoint "/health" "HEALTH"
call_endpoint "/version" "VERSION"
call_endpoint "/metrics" "METRICS"
