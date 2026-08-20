#!/usr/bin/env bash
# ~/.local/bin/restart.sh
set -euo pipefail

BASE_DIR=${QUEUE_BASE:-"/home/$USER/.local/share/queue"}
PENDING_DIR="$BASE_DIR/pending"
DONE_DIR="$BASE_DIR/done"
STATUS_DIR="$BASE_DIR/status"

JOB_ID="${1:-}"

if [ -z "$JOB_ID" ]; then
    echo "Error: Missing job ID." >&2
    echo "Usage: queue restart <job_id>" >&2
    exit 1
fi

# Ensure the job exists strictly in the done/ directory
if [ ! -f "$DONE_DIR/$JOB_ID" ]; then
    echo "Error: Job '$JOB_ID' is not in the done lane. Only completed jobs can be restarted." >&2
    exit 1
fi

# Move file back from done/ to pending/
mv "$DONE_DIR/$JOB_ID" "$PENDING_DIR/$JOB_ID"

# Append 'restarted' status to the job's audit log
jq -nc \
    --arg timestamp "$(date +'%Y-%m-%d %H:%M:%S')" \
    --arg id "$JOB_ID" \
    --arg status "restarted" \
    '$ARGS.named' >> "$STATUS_DIR/$JOB_ID.jsonl"

echo "Job '$JOB_ID' successfully moved from done to pending."