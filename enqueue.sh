#!/usr/bin/env bash
# ~/.local/bin/enqueue.sh
set -euo pipefail

BASE_DIR=${QUEUE_BASE:-"/home/$USER/.local/share/queue"}
PENDING_DIR=${PENDING_DIR:-"$BASE_DIR/pending"}
STATUS_DIR=${STATUS_DIR:-"$BASE_DIR/status"}
TASKS_DIR=${TASKS_DIR:-"$BASE_DIR/tasks"}
STAGING_DIR=${STAGING_DIR:-"$BASE_DIR/.staging"}

mkdir -p "$PENDING_DIR" "$STATUS_DIR" "$TASKS_DIR" "$STAGING_DIR"

if [ "$#" -eq 0 ]; then
	echo "Error: No command or template provided to enqueue." >&2
	echo "Usage: enqueue [-e|--edit <template.yaml>] [runner_command...]" >&2
	exit 1
fi

JOB_ID="$(date +'%Y%m%d_%H%M%S_%N')_$$"
TARGET_FILE=""

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
	trap 'rm -f "$TMP_STAGING"' EXIT

	# Abort if editor returns an error (e.g. :cq in Vim)
	if ! ${EDITOR:-vim} "$TMP_STAGING"; then
		echo "Aborted: editor exited with non-zero status." >&2
		rm -f "$TMP_STAGING"
		exit 1
	fi

	# Atomically move the edited task into durable task storage
	mv "$TMP_STAGING" "$FINAL_TASK"
	TARGET_FILE="$FINAL_TASK"

	# Construct execution command targeting the runner binary
	RUNNER_CMD=("task" "$FINAL_TASK" "$@")
	TASK=$(printf '%q ' "${RUNNER_CMD[@]}")
	TASK="${TASK% }"
else
	# --- Standard Command Enqueue Mode ---
	TASK=$(printf '%q ' "$@")
	TASK="${TASK% }"
	if [[ "${1:-}" =~ \.ya?ml$ && -f "$1" ]]; then
		TARGET_FILE="$1"
	fi
fi

FOLLOW=0
if [[ "$*" =~ (^|[[:space:]])(-f|--follow)($|[[:space:]]) ]]; then
	FOLLOW=1
fi

# Extract location using yq (checks nested values.definition or root definition)
LOCATION="local"
if [[ -n "$TARGET_FILE" && -f "$TARGET_FILE" ]]; then
	LOCATION=$(yq -r '.values.definition.location // .definition.location // "local"' "$TARGET_FILE")
fi

# --- Dispatch Route: Remote Desktop ---
if [[ "$LOCATION" == "desktop" ]]; then
	echo "Target node is 'desktop'. Dispatching remotely..." >&2
	REMOTE_HOST="desktop"
	REMOTE_QUEUE="~/.local/share/queue"

	# 1. Ensure remote directory tree exists
	ssh "$REMOTE_HOST" "mkdir -p $REMOTE_QUEUE/tasks $REMOTE_QUEUE/pending $REMOTE_QUEUE/status"

	# 2. Transfer task YAML to desktop storage
	scp "$TARGET_FILE" "$REMOTE_HOST:$REMOTE_QUEUE/tasks/$JOB_ID.yaml"

	# 3. Enqueue directly into desktop's pending lane and initialize remote status log
	ssh "$REMOTE_HOST" "
		echo $(printf '%q' "$TASK") > $REMOTE_QUEUE/pending/$JOB_ID
		jq -nc \
			--arg timestamp \"\$(date +'%Y-%m-%d %H:%M:%S')\" \
			--arg id \"$JOB_ID\" \
			--arg task $(printf '%q' "$TASK") \
			--arg status \"pending\" \
			'\$ARGS.named' >> $REMOTE_QUEUE/status/$JOB_ID.jsonl
	"

	# 4. Record local audit record that it was dispatched
	jq -nc \
		--arg timestamp "$(date +'%Y-%m-%d %H:%M:%S')" \
		--arg id "$JOB_ID" \
		--arg task "$TASK" \
		--arg status "dispatched" \
		--arg location "desktop" \
		'$ARGS.named' >>"$STATUS_DIR/$JOB_ID.jsonl"

	# 5. Follow remotely over SSH or print output
	if [ "$FOLLOW" -eq 1 ]; then
		exec ssh -t "$REMOTE_HOST" "queue status $JOB_ID"
	else
		ssh "$REMOTE_HOST" "jq . $REMOTE_QUEUE/status/$JOB_ID.jsonl"
	fi
	exit 0
fi

# --- Dispatch Route: Local Queue ---
echo "$TASK" >"$PENDING_DIR/$JOB_ID"

jq -nc \
	--arg timestamp "$(date +'%Y-%m-%d %H:%M:%S')" \
	--arg id "$JOB_ID" \
	--arg task "$TASK" \
	--arg status "pending" \
	'$ARGS.named' >>"$STATUS_DIR/$JOB_ID.jsonl"

if [ "$FOLLOW" -eq 1 ]; then
	exec queue status "$JOB_ID"
else
	jq . "$STATUS_DIR/$JOB_ID.jsonl"
fi
