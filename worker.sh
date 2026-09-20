#!/usr/bin/env bash

PENDING_DIR=${PENDING_DIR:-"/home/$USER/.local/share/queue/pending"}
STATUS_DIR=${STATUS_DIR:-"/home/$USER/.local/share/queue/status"}
PROC_DIR=${PROC_DIR:-"/home/$USER/.local/share/queue/processing"}
DONE_DIR=${DONE_DIR:-"/home/$USER/.local/share/queue/done"}
TASKS_DIR=${TASKS_DIR:-"/home/$USER/.local/share/queue/tasks"}

mkdir -p "$PENDING_DIR" "$STATUS_DIR" "$PROC_DIR" "$DONE_DIR"

if ! command -v inotifywait >/dev/null 2>&1; then
	echo "Error: inotify-tools missing. Install with: sudo apt install inotify-tools" >&2
	exit 1
fi

JOB_ID=""
CONCLUSION=""

dequeue() {
	JOB_ID=""
	shopt -s nullglob
	set -- "$PENDING_DIR"/*

	if [ -n "${1:-}" ] && [ -f "$1" ]; then
		local id
		id="$(basename "$1")"
		if mv "$1" "$PROC_DIR/$id" 2>/dev/null; then
			JOB_ID="$id"
		fi
	fi
}

run() {
	jq -nc \
		--arg timestamp "$(date +'%Y-%m-%d %H:%M:%S')" \
		--arg id "$JOB_ID" \
		--arg status "running" \
		'$ARGS.named' >>"$STATUS_DIR/$JOB_ID.jsonl"

	local args_file="$TASKS_DIR/$JOB_ID.args.json"
	local -a task_args=()
	if [[ -f "$args_file" ]]; then
		if ! jq -e 'type == "array" and all(.[]; type == "string" and (contains("\u0000") | not))' "$args_file" >/dev/null; then
			echo "Invalid task arguments: $args_file" >&2
			CONCLUSION=1
			return
		fi
		mapfile -d '' -t task_args < <(jq -j '.[] | . + "\u0000"' "$args_file")
	fi
	task "$TASKS_DIR/$JOB_ID.yaml" "${task_args[@]}"
	CONCLUSION=$?
}

finish() {
	local status_label="success"
	[ "$CONCLUSION" -ne 0 ] && status_label="failure"

	jq -nc \
		--arg timestamp "$(date +'%Y-%m-%d %H:%M:%S')" \
		--arg id "$JOB_ID" \
		--arg status "$status_label" \
		--arg exit_code "$CONCLUSION" \
		'$ARGS.named' >>"$STATUS_DIR/$JOB_ID.jsonl"

	mv "$PROC_DIR/$JOB_ID" "$DONE_DIR/$JOB_ID"

	JOB_ID=""
	CONCLUSION=""
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
