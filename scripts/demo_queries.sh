#!/bin/bash
# demo_queries.sh — Leadership demo script
# Runs the 3 demo scenarios against the live /ask endpoint
# Usage: bash scripts/demo_queries.sh
# Prerequisite: RAG API must be running on port 8000

set -e

ENDPOINT="http://localhost:8000/ask"
DIVIDER="================================================"

# Check API is reachable before starting
if ! curl -s --connect-timeout 5 "http://localhost:8000/health" > /dev/null; then
  echo "ERROR: API not reachable at http://localhost:8000"
  echo "Start it with: uvicorn rag.app:app --host 0.0.0.0 --port 8000"
  exit 1
fi

ask() {
  local question=$1
  curl -s -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d "{\"question\": \"$question\"}" \
    | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(d['answer'])
print('')
print('Sources:')
for s in d.get('sources', []):
    print(f\"  - {s['repo']} → {s['file']} (lines {s['lines']})\")
if d.get('graph_context_used'):
    print('  (service dependency graph also used)')
"
}

echo ""
echo "$DIVIDER"
echo " DEMO 1 — Understanding a full workflow"
echo "$DIVIDER"
echo "Question: How does the payment flow work end to end?"
echo ""
ask "How does the payment flow work end to end?"

echo ""
echo "$DIVIDER"
echo " DEMO 2 — Finding specific code across repos"
echo "$DIVIDER"
echo "Question: Where is the RabbitMQ publisher for payment events and who consumes it?"
echo ""
ask "Where is the RabbitMQ publisher for payment events and who consumes it?"

echo ""
echo "$DIVIDER"
echo " DEMO 3 — Impact analysis before making a change"
echo "$DIVIDER"
echo "Question: If I change PaymentService, what other services might be affected?"
echo ""
ask "If I change PaymentService, what other services might be affected?"

echo ""
echo "$DIVIDER"
echo " Demo complete"
echo "$DIVIDER"
