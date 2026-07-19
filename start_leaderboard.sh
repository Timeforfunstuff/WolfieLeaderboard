#!/usr/bin/env bash
# TikTok Live Leaderboard — launcher
# Uses the Python 3.11 venv with PirateTok (zero signing dependency).
# Auto-restarts the server if it crashes (keeps the overlay alive).
# Run at boot:  nohup ~/Projects/TikTok\ Live\ Leaderboard/start_leaderboard.sh Magiieee >/dev/null 2>&1 &

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# Use the venv (Python 3.11 + piratetok-live-py)
source "$DIR/.venv311/bin/activate"

USERNAME="${1:-Magiieee}"
LOG="$DIR/leaderboard.log"

# Kill any stray instance already holding port 8766 (avoids bind errors on reboot)
pkill -f "leaderboard_server.py" 2>/dev/null || true
sleep 1

echo "[LAUNCH] Starting leaderboard for @$USERNAME (PirateTok, no sign server)"
echo "[LAUNCH] Logs: $LOG"

# Auto-restart loop so the overlay never dies
while true; do
    echo "[LAUNCH] $(date '+%Y-%m-%d %H:%M:%S') — starting..." >> "$LOG"
    python -u leaderboard_server.py --user "$USERNAME" >> "$LOG" 2>&1
    echo "[LAUNCH] $(date '+%Y-%m-%d %H:%M:%S') — exited ($?). Restarting in 3s..." >> "$LOG"
    sleep 3
done
