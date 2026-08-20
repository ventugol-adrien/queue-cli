#!/usr/bin/env bash
# ~/.local/bin/enqueue.sh
set -euo pipefail

PENDING_DIR=${PENDING_DIR:-"/home/$USER/.local/share/queue/pending"}
STATUS_DIR=${STATUS_DIR:-"/home/$USER/.local/share/queue/status"}

mkdir -p "$PENDING_DIR" "$STATUS_DIR"

if [ "$#" -eq 0 ]; then
    echo "Error: No command provided to enqueue." >&2
    echo "Usage: queue enqueue <command...>" >&2
    exit 1
fi

# High-resolution timestamp + PID ensures natural FIFO ordering without collisions
JOB_ID="$(date +'%Y%m%d_%H%M%S_%N')_$$"
TASK=$(printf '%q ' "$@")
TASK="${TASK% }"

# 1. Save task to pending lane
echo "$TASK" > "$PENDING_DIR/$JOB_ID"

# 2. Append structured pending record
jq -nc \
    --arg timestamp "$(date +'%Y-%m-%d %H:%M:%S')" \
    --arg id "$JOB_ID" \
    --arg task "$TASK" \
    --arg status "pending" \
    '$ARGS.named' >> "$STATUS_DIR/$JOB_ID.jsonl"

# 3. Print output
jq . "$STATUS_DIR/$JOB_ID.jsonl"