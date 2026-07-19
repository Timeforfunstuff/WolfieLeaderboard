@echo off
REM Auto-start TikTok Live Leaderboard server on Windows boot.
REM Runs the WSL launcher in the background (detached, no window).
wsl.exe -d Ubuntu-22.04 -e bash -c "nohup /mnt/c/Users/timef/AppData/Local/Packages/CanonicalGroupLimited.Ubuntu22.04LTS_79rhkp1fndgsc/LocalState/Projects/TikTok\ Live\ Leaderboard/start_leaderboard.sh Magiieee >/dev/null 2>&1 &"
exit /b 0
