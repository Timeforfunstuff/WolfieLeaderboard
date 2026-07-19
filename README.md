# TikTok Live Leaderboard

A real-time engagement leaderboard for TikTok livestreams. Gamifies viewer
engagement (gifts + likes) into a competitive Top 10 scoreboard, displayed as a
browser source overlay in TikTok Live Studio.

## How It Works

```
PirateTok (Python)  →  WebSocket (0.0.0.0:8765)  →  Browser overlay
```

- **Zero signing dependency** — uses [PirateTok](https://github.com/PirateTok/live-py)
  (no API key, no sign server, no EulerStream account required)
- **Gifts** are scored per-user (real per-viewer data from PirateTok)
- **Likes** (screen taps) are aggregated into a "Hype Meter" that boosts a
  global score multiplier — tapping the screen helps everyone climb
- Scores are **per-stream only** (in-memory, no persistence, resets on reconnect)

## Setup

### 1. Install dependencies (Python 3.11+ required)
```bash
# Create venv with Python 3.11
python3.11 -m venv .venv311
source .venv311/bin/activate
pip install piratetok-live-py websockets
```

### 2. Run the server
```bash
# Recommended: use the auto-restart launcher (keeps overlay alive)
./start_leaderboard.sh sk_wolfie

# Or run directly with the venv active:
source .venv311/bin/activate
python leaderboard_server.py --user sk_wolfie
```
Replace `sk_wolfie` with your actual TikTok handle (no `@` needed).

The server starts an HTTP server on port **8766** and a WebSocket on **8765**,
both bound to `0.0.0.0` (all interfaces).

### 3. Add to TikTok Live Studio
1. Open **TikTok Live Studio**
2. Add a **Browser Source** (or "Web Page" source)
3. URL: `http://<YOUR_LAN_IP>:8766/`
   - Find your LAN IP: `hostname -I | awk '{print $1}'`
   - Example: `http://123.12.123.123:8766/`
   - **Do NOT use `localhost`** — TikTok Live Studio's browser source sandbox
     blocks loopback addresses. Use your LAN IP instead.
   - (Your public/router IP will NOT work from inside your own network —
     NAT loopback is usually disabled on home routers.)
4. Set width: **420**, height: **600** (or fit your layout)
5. Make sure the server (step 2) is running before you go live

> Note: The browser source loads from your local machine — it only works
> while `leaderboard_server.py` is running on the same computer.
>
> Architecture: HTTP server on port **8766** (serves the page),
> WebSocket server on port **8765** (pushes live score updates).
> The WebSocket URL is auto-derived from the page host, so using your
> LAN IP in the browser-source URL automatically points the WS client
> at the same IP — no manual `localhost` references remain.

## Controls

- Scores reset automatically when the stream reconnects (new process)
- No manual reset needed — each go-live = fresh board

## Scoring

| Action | Points |
|--------|--------|
| Gift (per diamond) | `diamonds × 10 × hype_multiplier` |
| Likes (every 500) | Hype level +1 → multiplier increases |
| Follow | +50 (tracked, shown in stats) |

## Files

| File | Purpose |
|------|---------|
| `leaderboard_server.py` | TikTokLive listener + WebSocket server |
| `static/index.html` | Leaderboard UI |
| `static/style.css` | Neon/dark overlay styling |
| `static/app.js` | WebSocket client, live rendering |

## Notes / Limitations

- TikTokLive is an **unofficial** library (reverse-engineered). It may break
  if TikTok changes internals. For personal/streamer use this is widely used;
  not for commercial resale without legal review.
- TikTok does **not** expose per-user tap counts. Likes are aggregated, so the
  leaderboard is gift-driven with likes as a global hype boost.
- Requires Python 3.10+.

## Planned

- [ ] Configurable scoring weights
- [ ] Sound/visual alert on new #1
- [ ] Name animation on gift received
- [ ] OBS-compatible (already works as browser source)
