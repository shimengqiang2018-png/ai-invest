#!/bin/bash
# Watchdog: prevent Mac sleep + keep momentum-dashboard server alive
# Runs caffeinate to prevent sleep, and restarts server if it crashes

SERVER_DIR="/Users/shimengqiang/IdeaProjects/public/ai-invest"
PYTHON="/Users/shimengqiang/opt/miniconda3/bin/python3.13"
SERVER_SCRIPT="momentum-dashboard/server.py"

# Prevent display/idle/disk sleep in background
caffeinate -d -i -m &
CAFF_PID=$!

# Cleanup on exit
cleanup() {
    kill $CAFF_PID 2>/dev/null
    exit 0
}
trap cleanup SIGTERM SIGINT

# Main loop: restart server if it dies
while true; do
    if ! pgrep -f "momentum-dashboard/server.py" > /dev/null 2>&1; then
        echo "[$(date)] Server not running, starting..."
        cd "$SERVER_DIR" && "$PYTHON" "$SERVER_SCRIPT" &
        SERVER_PID=$!
        echo "[$(date)] Server started with PID $SERVER_PID"
        wait $SERVER_PID
        echo "[$(date)] Server exited with code $?, restarting in 3s..."
        sleep 3
    else
        sleep 10
    fi
done
