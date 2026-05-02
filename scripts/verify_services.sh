#!/bin/bash
# verify_services.sh — Check all Phase 1 services are running and healthy
#
# Usage: bash scripts/verify_services.sh
# Run from any directory — all URLs are hardcoded to localhost.
#
# Exit codes:
#   0 — all Phase 1 services healthy
#   1 — one or more Phase 1 services failed
#
# Phase 3 services (FastAPI) are checked separately via check_optional()
# and do NOT affect the exit code. They are expected to fail until Phase 3.

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

PASS=0
FAIL=0

# check — curl a service and increment PASS/FAIL counter
# Args: $1=display name  $2=URL  $3=expected HTTP status code
# Affects exit code of this script via the FAIL counter.
check() {
  local name=$1
  local url=$2
  local expected=$3

  response=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$url")
  if [[ "$response" == "$expected" ]]; then
    echo -e "${GREEN}PASS${NC} $name ($url)"
    ((PASS++))
  else
    echo -e "${RED}FAIL${NC} $name ($url) — got HTTP $response, expected $expected"
    ((FAIL++))
  fi
}

# check_optional — curl a service but do NOT affect PASS/FAIL counter
# Used for services not yet built. Prints informational status only.
# Args: $1=display name  $2=URL  $3=expected HTTP status code
check_optional() {
  local name=$1
  local url=$2
  local expected=$3

  response=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 "$url")
  if [[ "$response" == "$expected" ]]; then
    echo -e "${GREEN}PASS${NC} $name ($url)"
  else
    # Use YELLOW for optional checks — not a failure, just not built yet
    echo -e "${YELLOW}NOT YET${NC} $name ($url) — got HTTP $response (not built until Phase 3)"
  fi
}

echo "Checking Phase 1 services..."
echo "------------------------"

check "Qdrant"     "http://localhost:6333/healthz"        "200"
check "Ollama"     "http://localhost:11434"                "200"
check "Neo4j"      "http://localhost:7474"                 "200"
check "MinIO"      "http://localhost:9000/minio/health/live" "200"
check "Open WebUI" "http://localhost:3000"                 "200"

echo "------------------------"
echo "Results: ${PASS} PASS, ${FAIL} FAIL"

if [ $FAIL -gt 0 ]; then
  echo "Some services are not running. Check: docker compose logs [service-name]"
fi

# Sourcegraph — optional because it takes 2-3 minutes to start and requires manual setup wizard
echo ""
echo "Sourcegraph (optional — may need 2-3 min startup + setup wizard):"
echo "------------------------"
check_optional "Sourcegraph" "http://localhost:7080/healthz" "200"
echo "------------------------"

# Phase 3 services — checked for information only, do not affect exit code
echo ""
echo "Phase 3 services (expected NOT YET until Phase 3 is built):"
echo "------------------------"
check_optional "FastAPI" "http://localhost:8000/health" "200"
echo "------------------------"

# Exit based only on Phase 1 results
if [ $FAIL -gt 0 ]; then
  exit 1
else
  echo "All Phase 1 services healthy."
  exit 0
fi
