#!/usr/bin/env bash
# Create strategies/<strategy-id>/ from strategies/_template/.
# Usage: scripts/new-strategy.sh <strategy-id>
set -euo pipefail

id="${1:?usage: scripts/new-strategy.sh <strategy-id>}"
if [[ ! "$id" =~ ^[a-z0-9]+(-[a-z0-9]+)*$ ]]; then
    echo "strategy-id must be lowercase letters, digits, and single hyphens: $id" >&2
    exit 1
fi

root="$(cd "$(dirname "$0")/.." && pwd)"
template="$root/strategies/_template"
target="$root/strategies/$id"
if [[ -e "$target" ]]; then
    echo "already exists: $target" >&2
    exit 1
fi

rsync -a --exclude .env --exclude .venv --exclude uv.lock --exclude __pycache__ \
    "$template/" "$target/"

for f in pyproject.toml README.md; do
    sed "s/strategy-template/$id/g" "$template/$f" > "$target/$f"
done

echo "Created strategies/$id"
echo "Next: cd strategies/$id && uv sync && cp .env.example .env && uv run strategy"
