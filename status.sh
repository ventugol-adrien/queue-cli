#!/usr/bin/env bash
# ~/.local/bin/status.sh
set -euo pipefail

BASE_DIR=${QUEUE_BASE:-"/home/$USER/.local/share/queue"}
STATUS_DIR="$BASE_DIR/status"

# If a specific Job ID is passed, show its full history
if [ -n "${1:-}" ]; then
    JOB_LOG="$STATUS_DIR/$1.jsonl"
    if [ -f "$JOB_LOG" ]; then
        echo "=== Audit History for Job: $1 ==="
        tail -n +1 -f "$JOB_LOG" | while IFS= read -r line; do
            echo "$line"
            status=$(echo "$line" | jq -r '.status // empty')
            if [[ "$status" == "success" || "$status" == "failure" ]]; then
                echo "=== Job finalized with status: $status ==="
                break
            fi
        done
        exit 0
    else
        echo "Error: Job ID '$1' not found in $STATUS_DIR" >&2
        exit 1
    fi
    exit 0
fi

# Otherwise, print 3-lane summary dashboard
echo "================ Queue Status ================"
printf "%-15s : %d\n" "Pending"    "$(find "$BASE_DIR/pending" -maxdepth 1 -type f 2>/dev/null | wc -l)"
printf "%-15s : %d\n" "Processing" "$(find "$BASE_DIR/processing" -maxdepth 1 -type f 2>/dev/null | wc -l)"
printf "%-15s : %d\n" "Done"       "$(find "$BASE_DIR/done" -maxdepth 1 -type f 2>/dev/null | wc -l)"
echo "=============================================="