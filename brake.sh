#!/usr/bin/env bash
# ~/.local/bin/brake.sh
set -euo pipefail

if [[ $# -eq 0 ]]; then
    echo "Error: No job ID provided." >&2
    echo "Usage: brake.sh <job_id>" >&2
    exit 1
fi 

JOB_ID="$1"
BASE_DIR=${QUEUE_BASE:-"/home/$USER/.local/share/queue"}
PENDING_DIR=${PENDING_DIR:-"$BASE_DIR/pending"}
PROC_DIR=${PROC_DIR:-"$BASE_DIR/processing"}
DONE_DIR=${DONE_DIR:-"$BASE_DIR/done"}
STATUS_DIR=${STATUS_DIR:-"$BASE_DIR/status"}

if [[ ! -f "$STATUS_DIR/$JOB_ID.jsonl" ]]; then
    echo "Error: No status history found for job ID '$JOB_ID'." >&2
    exit 1
fi

# --- Case 1: Job is still queued in Pending ---
if [[ -f "$PENDING_DIR/$JOB_ID" ]]; then
    echo "Job '$JOB_ID' is currently pending. Removing from queue..."
    mv "$PENDING_DIR/$JOB_ID" "$DONE_DIR/$JOB_ID"

    jq -nc \
        --arg timestamp "$(date +'%Y-%m-%d %H:%M:%S')" \
        --arg id "$JOB_ID" \
        --arg status "cancelled" \
        --arg reason "Job cancelled by user before execution" \
        '$ARGS.named' >> "$STATUS_DIR/$JOB_ID.jsonl"

    echo "Job '$JOB_ID' cancelled successfully."
    exit 0
fi

# --- Case 2: Job is actively Processing ---
if [[ -f "$PROC_DIR/$JOB_ID" ]]; then
    echo "Job '$JOB_ID' is actively running. Locating process..."

    # Find matching PIDs, excluding the brake script's own process ID
    PIDS=$(pgrep -f "$JOB_ID" | grep -v "^$$\$" || true)

    if [[ -z "$PIDS" ]]; then
        echo "Warning: Job is in processing lane, but no running process matched '$JOB_ID'." >&2
        exit 1
    fi

    echo "Sending SIGTERM to process(es): $PIDS"
    # shellcheck disable=SC2086
    kill -15 $PIDS 2>/dev/null || true

    # Give the process 2 seconds to gracefully exit
    sleep 2

    # Check if any processes are still alive and force kill if necessary
    for pid in $PIDS; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "Process $pid did not exit; sending SIGKILL..."
            kill -9 "$pid" 2>/dev/null || true
        fi
    done

    echo "Job '$JOB_ID' terminated."
    exit 0
fi

# --- Case 3: Job is already completed ---
if [[ -f "$DONE_DIR/$JOB_ID" ]]; then
    echo "Job '$JOB_ID' has already finished. Nothing to stop."
    exit 0
fi