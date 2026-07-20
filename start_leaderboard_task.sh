#!/bin/bash
cd "/mnt/c/Users/timef/AppData/Local/Packages/CanonicalGroupLimited.Ubuntu22.04LTS_79rhkp1fndgsc/LocalState/Projects/TikTok Live Leaderboard"
source .venv311/bin/activate
exec python -B -u leaderboard_server.py --user sk_wolfie
