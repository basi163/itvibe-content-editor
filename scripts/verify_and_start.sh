#!/usr/bin/env bash
set -euo pipefail

project_dir="${1:-/opt/itvibe-content-editor}"
cd "$project_dir"

test -f .env
chmod 600 .env

docker build --target test -t itvibe-content-editor:test .
docker compose config --quiet
docker compose up -d --build
docker compose ps
