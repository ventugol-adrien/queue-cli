#!/usr/bin/env bash
# ~/.local/bin/enqueue.sh
set -euo pipefail

PENDING_DIR=${PENDING_DIR:-"/home/$USER/.local/share/queue/pending"}
STATUS_DIR=${STATUS_DIR:-"/home/$USER/.local/share/queue/status"}
TASKS_DIR=${TASKS_DIR:-"/home/$USER/.local/share/queue/tasks"}
STAGING_DIR=${STAGING_DIR:-"/home/$USER/.local/share/queue/.staging"}

mkdir -p "$PENDING_DIR" "$STATUS_DIR" "$TASKS_DIR" "$STAGING_DIR"

if [ "$#" -eq 0 ]; then
    echo "Error: No command or template provided to enqueue." >&2
    echo "Usage: enqueue [-e|--edit <template.yaml>] [runner_command...]" >&2
    exit 1
fi

JOB_ID="$(date +'%Y%m%d_%H%M%S_%N')_$$"

# --- Interactive Edit Mode ---
if [[ "$1" == "-e" || "$1" == "--edit" ]]; then
    shift
    TEMPLATE="${1:-}"
    if [[ -z "$TEMPLATE" || ! -f "$TEMPLATE" ]]; then
        echo "Error: Template file '$TEMPLATE' does not exist." >&2
        exit 1
    fi
    shift

    TMP_STAGING="$STAGING_DIR/$JOB_ID.yaml"
    FINAL_TASK="$TASKS_DIR/$JOB_ID.yaml"

    cp "$TEMPLATE" "$TMP_STAGING"

    # Abort if editor returns an error (e.g. :cq in Vim)
    if ! ${EDITOR:-vim} "$TMP_STAGING"; then
        echo "Aborted: editor exited with non-zero status." >&2
        rm -f "$TMP_STAGING"
        exit 1
    fi

    # Atomically move the edited task into durable task storage
    mv "$TMP_STAGING" "$FINAL_TASK"

    # Construct execution command targeting the runner binary
    # Extra CLI arguments passed after the template become flags to runner.py
    RUNNER_CMD=("task" "$FINAL_TASK" "$@")
    TASK=$(printf '%q ' "${RUNNER_CMD[@]}")
    TASK="${TASK% }"
else
    # --- Standard Command Enqueue Mode ---
    TASK=$(printf '%q ' "$@")
    TASK="${TASK% }"
fi

FOLLOW=0
if [[ "$*" =~ (^|[[:space:]])(-f|--follow)($|[[:space:]]) ]]; then
    FOLLOW=1
fi

# 1. Save execution command to pending lane
echo "$TASK" > "$PENDING_DIR/$JOB_ID"

# 2. Append structured pending record
jq -nc \
    --arg timestamp "$(date +'%Y-%m-%d %H:%M:%S')" \
    --arg id "$JOB_ID" \
    --arg task "$TASK" \
    --arg status "pending" \
    '$ARGS.named' >> "$STATUS_DIR/$JOB_ID.jsonl"

# 3. Print output
if [ "$FOLLOW" -eq 1 ]; then
    exec queue status "$JOB_ID"
else
    jq . "$STATUS_DIR/$JOB_ID.jsonl"
fi