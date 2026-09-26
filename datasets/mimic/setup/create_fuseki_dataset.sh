#!/usr/bin/env bash
# Create the MIMIC-III Fuseki dataset — issue #53
# Run after Fuseki is up: docker compose up fuseki -d

set -euo pipefail

FUSEKI_URL="${FUSEKI_URL:-http://localhost:3030}"
FUSEKI_ADMIN_PASSWORD="${FUSEKI_ADMIN_PASSWORD:-admin}"
DATASET="${RDF_DATASET_NAME:-mimic-mesh}"

echo "Creating Fuseki dataset: ${DATASET} at ${FUSEKI_URL}"

HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
  -u "admin:${FUSEKI_ADMIN_PASSWORD}" \
  -X POST "${FUSEKI_URL}/\$/datasets" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  --data-urlencode "dbName=${DATASET}" \
  --data-urlencode "dbType=tdb2")

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
  echo "Dataset '${DATASET}' created successfully."
elif [ "$HTTP_CODE" = "409" ]; then
  echo "Dataset '${DATASET}' already exists — skipping."
else
  echo "Unexpected HTTP status: ${HTTP_CODE}" >&2
  exit 1
fi
