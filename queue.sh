SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"

# Source the configuration function if it lives in your functions directory
if [ -f "$HOME/.config/bash/functions/queue-cli.sh" ]; then
    . "$HOME/.config/bash/functions/queue-cli.sh"
fi

queue-cli

case "${1:-}" in
    enqueue|status|restart)
        ACTION="$1"
        shift
        exec "$SCRIPT_DIR/$ACTION.sh" "$@"
        ;;
    *)
        echo "Usage: $0 {enqueue|status|restart}"
        exit 1
        ;;
esac