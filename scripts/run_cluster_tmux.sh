#!/usr/bin/env bash
# run_cluster_tmux.sh — Local 6-node NTP Cluster Orchestrator via tmux
#
# Topology:
#   Pane 0: Stratum 1 Master (port 5001)
#   Pane 1: Stratum 2 Relay A (port 5002 -> 5001)
#   Pane 2: Stratum 2 Relay B (port 5003 -> 5001)
#   Pane 3: Stratum 3 Client A (port 5004 -> 5002, 5003)
#   Pane 4: Stratum 3 Client B (port 5005 -> 5002, 5003)
#   Pane 5: Stratum 3 Client C (port 5006 -> 5002, 5003)

set -uo pipefail

SESSION_NAME="${SESSION_NAME:-ntp-cluster}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Verify tmux is available
if ! command -v tmux >/dev/null 2>&1; then
    echo "Error: tmux is required but not installed." >&2
    echo "Please install tmux (e.g., 'sudo apt install tmux' or 'brew install tmux')." >&2
    exit 1
fi

show_help() {
    cat <<EOF
Usage: $0 [COMMAND] [OPTIONS]

Orchestrate a local 6-node NTP simulation cluster in a tmux tiled grid.

Commands:
  start     Start the 6-node cluster in tmux (default if omitted)
  stop      Gracefully shut down the cluster tmux session
  kill      Alias for stop
  restart   Stop and restart the cluster session
  attach    Attach to the running tmux session
  status    Check running state of the cluster session
  help      Display this help message

Options:
  --no-attach, -d    Start in detached mode without attaching (for 'start')

Environment Variables:
  SESSION_NAME       Name of tmux session (default: ntp-cluster)
EOF
}

cmd_status() {
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "Cluster status: RUNNING (tmux session '$SESSION_NAME' active)"
        tmux list-panes -t "$SESSION_NAME" -F '  Pane #{pane_index}: #{pane_title}' 2>/dev/null || true
        return 0
    else
        echo "Cluster status: STOPPED (tmux session '$SESSION_NAME' not found)"
        return 0
    fi
}

cmd_stop() {
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true
        echo "Cluster session '$SESSION_NAME' stopped."
    else
        echo "Cluster session '$SESSION_NAME' is not running."
    fi
}

cmd_attach() {
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        exec tmux attach-session -t "$SESSION_NAME"
    else
        echo "Error: Cluster session '$SESSION_NAME' is not running." >&2
        echo "Run '$0 start' to start the cluster." >&2
        exit 1
    fi
}

cmd_start() {
    if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
        echo "Session '$SESSION_NAME' is already running."
        echo "Use '$0 attach' to connect, or '$0 stop' to terminate."
        return 0
    fi

    local should_attach=1
    for arg in "$@"; do
        if [ "$arg" = "--no-attach" ] || [ "$arg" = "-d" ] || [ "$arg" = "--detach" ]; then
            should_attach=0
        fi
    done

    echo "Starting 6-node NTP cluster in tmux session '$SESSION_NAME'..."

    # Create detached session with generous window dimensions to allow 6-pane splitting
    tmux new-session -d -s "$SESSION_NAME" -x 220 -y 60 -c "$REPO_ROOT"
    tmux set-environment -t "$SESSION_NAME" PYTHONPATH "$REPO_ROOT" 2>/dev/null || true

    # Split into 6 panes (initial pane + 5 splits) with tiled layout re-evaluation
    tmux select-layout -t "$SESSION_NAME" tiled
    for _ in {1..5}; do
        tmux split-window -t "$SESSION_NAME" -c "$REPO_ROOT"
        tmux select-layout -t "$SESSION_NAME" tiled
    done

    # Get ordered pane IDs
    panes=($(tmux list-panes -t "$SESSION_NAME" -F '#{pane_id}'))

    # Commands for each stratum node
    cmd_p0="python3 -m ntp_simulation --stratum 1 --port 5001"
    cmd_p1="python3 -m ntp_simulation --stratum 2 --port 5002 --upstream 127.0.0.1:5001"
    cmd_p2="python3 -m ntp_simulation --stratum 2 --port 5003 --upstream 127.0.0.1:5001"
    cmd_p3="python3 -m ntp_simulation --stratum 3 --port 5004 --upstream 127.0.0.1:5002 --upstream 127.0.0.1:5003"
    cmd_p4="python3 -m ntp_simulation --stratum 3 --port 5005 --upstream 127.0.0.1:5002 --upstream 127.0.0.1:5003"
    cmd_p5="python3 -m ntp_simulation --stratum 3 --port 5006 --upstream 127.0.0.1:5002 --upstream 127.0.0.1:5003"

    # Set pane titles
    tmux select-pane -t "${panes[0]}" -T "Stratum 1 Master (5001)"
    tmux select-pane -t "${panes[1]}" -T "Stratum 2 Relay A (5002)"
    tmux select-pane -t "${panes[2]}" -T "Stratum 2 Relay B (5003)"
    tmux select-pane -t "${panes[3]}" -T "Stratum 3 Client A (5004)"
    tmux select-pane -t "${panes[4]}" -T "Stratum 3 Client B (5005)"
    tmux select-pane -t "${panes[5]}" -T "Stratum 3 Client C (5006)"

    # Brief pause to ensure shells (e.g. zsh/bash) have finished motd initialization
    sleep 0.3

    # Send startup commands to each pane
    tmux send-keys -t "${panes[0]}" "$cmd_p0" C-m
    tmux send-keys -t "${panes[1]}" "$cmd_p1" C-m
    tmux send-keys -t "${panes[2]}" "$cmd_p2" C-m
    tmux send-keys -t "${panes[3]}" "$cmd_p3" C-m
    tmux send-keys -t "${panes[4]}" "$cmd_p4" C-m
    tmux send-keys -t "${panes[5]}" "$cmd_p5" C-m

    # Apply tiled layout and show pane borders with titles
    tmux select-layout -t "$SESSION_NAME" tiled
    tmux set-option -t "$SESSION_NAME" pane-border-status top 2>/dev/null || true
    tmux set-option -t "$SESSION_NAME" pane-border-format " [#{pane_index}] #{pane_title} " 2>/dev/null || true

    echo "Cluster started successfully in session '$SESSION_NAME'."
    echo "Topology:"
    echo "  [0] Stratum 1 Master   :5001"
    echo "  [1] Stratum 2 Relay A  :5002 -> 127.0.0.1:5001"
    echo "  [2] Stratum 2 Relay B  :5003 -> 127.0.0.1:5001"
    echo "  [3] Stratum 3 Client A :5004 -> 127.0.0.1:5002, 127.0.0.1:5003"
    echo "  [4] Stratum 3 Client B :5005 -> 127.0.0.1:5002, 127.0.0.1:5003"
    echo "  [5] Stratum 3 Client C :5006 -> 127.0.0.1:5002, 127.0.0.1:5003"
    echo ""
    echo "Run '$0 attach' to view the cluster, or '$0 stop' to shut it down."

    if [ -t 0 ] && [ -t 1 ] && [ "$should_attach" -eq 1 ] && [ "${NO_ATTACH:-0}" != "1" ]; then
        exec tmux attach-session -t "$SESSION_NAME"
    fi
}

cmd_restart() {
    echo "Restarting cluster '$SESSION_NAME'..."
    cmd_stop
    sleep 0.5
    cmd_start "$@"
}

# Main command dispatch
ACTION="${1:-start}"
case "$ACTION" in
    start)
        shift || true
        cmd_start "$@"
        ;;
    stop|kill)
        cmd_stop
        ;;
    restart)
        shift || true
        cmd_restart "$@"
        ;;
    attach)
        cmd_attach
        ;;
    status)
        cmd_status
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        echo "Error: Unknown command '$ACTION'" >&2
        echo "Usage: $0 [start|stop|kill|restart|attach|status|help]" >&2
        exit 1
        ;;
esac
