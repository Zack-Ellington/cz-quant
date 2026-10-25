#!/usr/bin/env bash
set -euo pipefail
: "${STRATEGY_RUNS_DIR:?}" "${CZQUANT_BUCKET:?}"
repo_dir=$(cd -- "$(dirname -- "$0")/../.." && pwd)
archive_stamp=$(date -u +%Y%m%dT%H%M%SZ)
archive_dir="$STRATEGY_RUNS_DIR/archives/$archive_stamp"
mkdir -p "$archive_dir"
"$repo_dir/packages/czquant/.venv/bin/czquant" --runs-dir "$STRATEGY_RUNS_DIR" backup --output "$archive_dir/accounting.sqlite3"
cp "$STRATEGY_RUNS_DIR/trial.json" "$archive_dir/trial.json"
(cd "$archive_dir" && sha256sum accounting.sqlite3 trial.json > SHA256SUMS)
gzip "$archive_dir/accounting.sqlite3"
printf 'Restore with: gunzip -k accounting.sqlite3.gz\nThen verify: sha256sum -c SHA256SUMS\n' > "$archive_dir/RESTORE.txt"
gcloud storage cp --recursive "$archive_dir" "$CZQUANT_BUCKET/archives/"
