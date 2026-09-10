#!/usr/bin/env bash

PENDING_DIR=${PENDING_DIR:-"/home/$USER/.local/share/queue/pending"}
STATUS_DIR=${STATUS_DIR:-"/home/$USER/.local/share/queue/status"}
PROC_DIR=${PROC_DIR:-"/home/$USER/.local/share/queue/processing"}
DONE_DIR=${DONE_DIR:-"/home/$USER/.local/share/queue/done"}

mkdir -p "$PENDING_DIR" "$STATUS_DIR" "$PROC_DIR" "$DONE_DIR"

if ! command -v inotifywait >/dev/null 2>&1; then
    echo "Error: inotify-tools missing. Install with: sudo apt install inotify-tools" >&2
    exit 1
fi

JOB_ID=""
CONCLUSION=""

# Notification Tracking State
NOTIF_ID=""
BATCH_TOTAL=0

update_notification() {
    local remaining
    remaining=$(find "$PENDING_DIR" "$PROC_DIR" -maxdepth 1 -type f 2>/dev/null | wc -l)

    # If new batch of tasks arrived, reset baseline
    if [ "$remaining" -gt "$BATCH_TOTAL" ]; then
        BATCH_TOTAL=$remaining
    fi

    if [ "$remaining" -gt 0 ] && [ "$BATCH_TOTAL" -gt 0 ]; then
        # Calculate remaining percentage for decreasing bar (100% -> 0%)
        local pct=$(( remaining * 100 / BATCH_TOTAL ))

        if [ -z "$NOTIF_ID" ]; then
            # Create initial persistent notification
            NOTIF_ID=$(notify-send -p \
                -h int:value:"$pct" \
                -t 0 \
                -i "system-run" \
                "Task Queue Active" \
                "Remaining tasks: $remaining / $BATCH_TOTAL")
        else
            # Update existing notification in place
            notify-send -r "$NOTIF_ID" \
                -h int:value:"$pct" \
                -t 0 \
                -i "system-run" \
                "Task Queue Active" \
                "Remaining tasks: $remaining / $BATCH_TOTAL"
        fi
    elif [ -n "$NOTIF_ID" ]; then
        # Queue is empty: display completion notification that auto-fades in 4 seconds
        notify-send -r "$NOTIF_ID" \
            -h int:value:0 \
            -t 4000 \
            -i "emblem-default" \
            "Queue Complete" \
            "All $BATCH_TOTAL tasks finished."
        
        # Reset tracker
        NOTIF_ID=""
        BATCH_TOTAL=0
    fi
}

dequeue () {
    JOB_ID=""
    shopt -s nullglob
    set -- "$PENDING_DIR"/*

    if [ -n "${1:-}" ] && [ -f "$1" ]; then
        local id
        id="$(basename "$1")"
        if mv "$1" "$PROC_DIR/$id" 2>/dev/null; then
            JOB_ID="$id"
            update_notification
        fi
    fi
}



run () {
    jq -nc \
        --arg timestamp "$(date +'%Y-%m-%d %H:%M:%S')" \
        --arg id "$JOB_ID" \
        --arg status "running" \
        '$ARGS.named' >> "$STATUS_DIR/$JOB_ID.jsonl"

    task "$PROC_DIR/$JOB_ID.yaml"
    CONCLUSION=$?
}

finish () {
    local status_label="success"
    [ "$CONCLUSION" -ne 0 ] && status_label="failure"

    jq -nc \
        --arg timestamp "$(date +'%Y-%m-%d %H:%M:%S')" \
        --arg id "$JOB_ID" \
        --arg status "$status_label" \
        --arg exit_code "$CONCLUSION" \
        '$ARGS.named' >> "$STATUS_DIR/$JOB_ID.jsonl"

    mv "$PROC_DIR/$JOB_ID" "$DONE_DIR/$JOB_ID"

    JOB_ID=""
    CONCLUSION=""
    update_notification
}

# --- Event Loop ---
echo "Worker listening on $PENDING_DIR..."

while true; do
    dequeue

    if [ -n "$JOB_ID" ]; then
        run
        finish
    else
        inotifywait -qq -e close_write -e moved_to "$PENDING_DIR"
    fi
done