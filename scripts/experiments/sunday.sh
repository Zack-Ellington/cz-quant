#!/usr/bin/env bash
set -euo pipefail
: "${STRATEGY_RUNS_DIR:?}" "${CZQUANT_START:?}" "${CZQUANT_CUTOFF:?}"
repo_dir=$(cd -- "$(dirname -- "$0")/../.." && pwd)
report_dir="$STRATEGY_RUNS_DIR/reports/sunday-20261025"
"$repo_dir/packages/czquant/.venv/bin/czquant" --runs-dir "$STRATEGY_RUNS_DIR" report --since "$CZQUANT_START" --until "$CZQUANT_CUTOFF" --output "$report_dir"
if [[ -n "${CZQUANT_BUCKET:-}" ]]; then
    gcloud storage cp --recursive "$report_dir" "$CZQUANT_BUCKET/reports/"
fi
if [[ -n "${CZQUANT_PR:-}" ]]; then
    gh pr comment "$CZQUANT_PR" --repo Zack-Ellington/cz-quant --body-file "$report_dir/comparison.md"
fi
