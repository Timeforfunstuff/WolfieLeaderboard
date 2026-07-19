"""
TikTok Live Leaderboard — Server
Listens to a TikTok live stream via PirateTok (zero signing dependency),
scores gifts per-user, tracks aggregate likes as a Hype Meter,
and pushes live updates to browser clients via WebSocket.

Scores are per-stream (in-memory only, no persistence).

Requirements:
    pip install piratetok-live-py websockets

Usage:
    python leaderboard_server.py --user Magiieee
"""

import asyncio
import json
import argparse
from collections import defaultdict

# PirateTok: zero signing dependency — no API keys, no sign server, no auth.
from piratetok_live import TikTokLiveClient, EventType
from piratetok_live.helpers import GiftStreakTracker, LikeAccumulator
import websockets

# ---- Config ----
# Gifts: 1 diamond = 1 point (1:1, no scaling). See on_gift().
FOLLOW_POINTS = 50

# === HYPE METER IS RATE-BASED (not cumulative) ===
# TikTok reports `like_total` as a GLOBAL LIFETIME counter for the account, not
# per-stream. Deriving hype from it makes the meter randomly peg to x400+ every
# time the connection drops/reconnects. Instead, hype tracks the RATE of incoming
# engagement over a rolling window — so it reflects LIVE activity and drains
# smoothly when likes/gifts slow. This is immune to reconnect spikes.
# On top of the rate window, a 3s GRACE + 15s COUNTDOWN hold the meter steady
# after engagement stops, then drain it — so it doesn't jitter down mid-stream.
HYPE_STEP_MULTIPLIER = 0.1       # multiplier per step: x = 1.0 + steps*0.1
HYPE_MAX_STEPS = 40              # hard cap at x5.0 (level 40) — meter can't run away
HYPE_WINDOW_SECS = 10.0          # rolling window: only engagement from last 10s counts
HYPE_RATE_DIVISOR = 4            # weight units per 0.1 step. ~40 weight in window = x2.0, ~160 = x5.0
HYPE_GIFT_DIAMONDS_PER_STEP = 10  # 10 diamonds = 1 hype weight unit = +0.1 level (uncapped)
HYPE_LIKE_WEIGHT = 1             # hype weight per like (taps). Bursts of N taps = N weight (uncapped).
# Grace + countdown: after the last like/gift, hold the meter for GRACE_SECS,
# then run a visible COUNTDOWN_SECS ticker before it starts draining.
HYPE_GRACE_SECS = 3              # steady hold after last engagement
HYPE_COUNTDOWN_SECS = 15         # visible countdown before drain begins
# Legacy decay constants kept for compatibility with snapshot()/UI fields:
HYPE_COUNTDOWN_BASE = 15
HYPE_COUNTDOWN_MIN = 4
HYPE_COUNTDOWN_PER_LEVEL = 0.3

def hype_countdown_secs(level):
    return max(HYPE_COUNTDOWN_MIN, HYPE_COUNTDOWN_BASE - level * HYPE_COUNTDOWN_PER_LEVEL)
STALE_RECONNECT_SECS = 600         # very high: TikTok goes quiet for minutes between like/gift bursts during a LIVE stream. Only force-reconnect on truly dead connections; TikTok's own disconnect already drives the reconnect loop.

# ---- State (per-stream, in-memory) ----
scores = defaultdict(int)          # username -> points
gift_counts = defaultdict(int)     # username -> gift count
like_total = 0                     # raw display counter (lifetime-like, for the "likes" label only — NOT used for hype)
display_like_total = 0             # monotonic display counter, only ever increments within a stream
# === RATE-BASED HYPE ===
# hype_window holds (timestamp, weight) tuples for engagement in the last
# HYPE_WINDOW_SECS. hype_level is recomputed from the window sum each tick,
# so it climbs with live engagement and self-drains as old events expire.
hype_window = []                   # list of [ts, weight]
hype_level = 0                     # steps of 0.1 (0..HYPE_MAX_STEPS) — the DISPLAYED level
hype_target = 0                    # rate-based target from the live window (engagement-driven)
_decay_start_level = 0             # (unused in rate-based mode; kept for compat)
last_activity_time = 0             # asyncio-loop timestamp of last like/gift
last_event_time = 0                # asyncio-loop timestamp of last ANY TikTok event
hype_countdown_active = False     # kept for UI compat (always False in rate mode)
hype_countdown_start = 0          # (unused in rate mode)
follow_count = 0
recent_likers = []                 # list of recent liker display names (most recent first)
display_names = {}                 # uniqueId -> display name (nickname) for showing in UI
connected = False
stream_user = ""

from collections import deque

# WebSocket clients
CLIENTS = set()

# Gift streak tracker — de-dupes TikTok's cumulative combo events so each
# gift/diamond is counted exactly once (no double counting, no missing gifts).
gift_tracker = GiftStreakTracker()
# Like accumulator — reads TikTok's per-event `count` (delta) so a burst of N
# taps counts as N, not 1. TikTok's `total` is unreliable (jumps backwards).
like_tracker = LikeAccumulator()


def current_multiplier():
    return round(1.0 + hype_level * HYPE_STEP_MULTIPLIER, 2)


def _hype_add(weight):
    """Record an engagement event (weight) into the rolling hype window and
    update the rate-based TARGET. The displayed hype_level is pushed UP to the
    target immediately (engagement should raise the meter right away) and any
    pending grace/countdown is cancelled — so fresh likes/gifts hold the meter
    up. Immune to reconnect spikes because only events from the last
    HYPE_WINDOW_SECS count toward the target."""
    global hype_level, hype_target, hype_countdown_active, hype_countdown_start
    now = asyncio.get_event_loop().time()
    hype_window.append([now, weight])
    hype_target = _hype_window_level()
    # Engagement arrived: cancel any drain (grace/countdown) and lift the meter.
    hype_countdown_active = False
    hype_countdown_start = 0
    if hype_target > hype_level:
        hype_level = hype_target
        print(f"[HYPE] Level {hype_level} (x{current_multiplier()}) — {len(hype_window)} events in window", flush=True)


def _hype_window_level():
    """Compute the rate-based target level from events inside the window."""
    now = asyncio.get_event_loop().time()
    cutoff = now - HYPE_WINDOW_SECS
    while hype_window and hype_window[0][0] < cutoff:
        hype_window.pop(0)
    total_weight = sum(w for _, w in hype_window)
    return min(HYPE_MAX_STEPS, int(total_weight // HYPE_RATE_DIVISOR))


def _touch():
    """Record that a TikTok event arrived (used by stale watchdog + hype decay)."""
    global last_event_time, last_activity_time
    now = asyncio.get_event_loop().time()
    last_event_time = now
    last_activity_time = now  # any event (chat/join/like/gift) holds the hype meter up


def top_scores(n=10):
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:n]
    return [
        {"rank": i + 1, "user": display_names.get(u, u), "points": p, "gifts": gift_counts[u]}
        for i, (u, p) in enumerate(ranked)
    ]


def snapshot():
    now = asyncio.get_event_loop().time()
    if hype_level > 0 and hype_countdown_active and hype_countdown_start > 0:
        # In the decay countdown phase — flat HYPE_COUNTDOWN_SECS ticker.
        elapsed = now - hype_countdown_start
        countdown = max(0, int(HYPE_COUNTDOWN_SECS - elapsed))
    else:
        # Grace period (calm) or no hype — no visible countdown.
        countdown = 0
    return {
        "type": "state",
        "connected": connected,
        "stream_user": stream_user,
        "leaderboard": top_scores(10),
        "like_total": like_total,
        "hype_level": hype_level,
        "hype_multiplier": current_multiplier(),
        "hype_countdown": countdown,
        "hype_countdown_active": hype_countdown_active,
        "follow_count": follow_count,
        "recent_likers": recent_likers[:10],
    }


async def broadcast(data):
    """Send JSON to all connected browser clients."""
    if not CLIENTS:
        return
    msg = json.dumps(data)
    # websockets v16 removed the `.open` attribute; just send and let
    # return_exceptions swallow any connections that closed mid-flight.
    await asyncio.gather(
        *[c.send(msg) for c in list(CLIENTS)],
        return_exceptions=True,
    )


# ---- TikTokLive event handlers ----

def on_connect(evt):
    global connected
    connected = True
    _touch()
    print(f"[CONNECTED] Live: @{stream_user} (room {evt.data.get('room_id')})")
    asyncio.create_task(broadcast(snapshot()))


def on_disconnect(evt):
    global connected
    connected = False
    _touch()
    print("[DISCONNECTED] Stream ended or client closed")
    asyncio.create_task(broadcast(snapshot()))


def on_gift(evt):
    global last_activity_time
    data = evt.data
    _touch()
    last_activity_time = asyncio.get_event_loop().time()
    udata = data.get("user", {})
    uid = udata.get("uniqueId", "unknown")          # stable score key
    display = udata.get("nickname") or uid          # display name shown to viewers
    # De-dupe TikTok's cumulative combo events: each call returns ONLY the
    # new gifts/diamonds since the last event for this streak (delta).
    gevt = gift_tracker.process(data)
    points = gevt.event_diamond_count               # 1 diamond = 1 point, counted once
    gift_delta = gevt.event_gift_count
    if points <= 0:
        # No new diamonds this event (e.g. a mid-combo repeat already counted) —
        # still refresh the display name, but don't double score.
        display_names[uid] = display
        return
    scores[uid] += points
    gift_counts[uid] += gift_delta
    display_names[uid] = display
    # Gifts feed the HYPE meter at 1 weight unit per 10 diamonds (10 diamonds
    # = +0.1 hype level). UNCAPPED — a big gift scales linearly with its value.
    # weight = diamonds / 10
    _hype_add(points / HYPE_GIFT_DIAMONDS_PER_STEP)
    print(f"[GIFT] {display} (@{uid}) +{points}pts ({gift_delta} new x {data.get('gift', {}).get('name', '?')})")
    asyncio.create_task(broadcast(snapshot()))


def on_like(evt):
    global like_total, display_like_total, recent_likers, last_activity_time
    data = evt.data
    _touch()
    last_activity_time = asyncio.get_event_loop().time()
    # Read TikTok's RELIABLE per-event delta (`count`) via the accumulator.
    # TikTok batches taps into one event with count=N, so a burst of N taps
    # counts as N — not 1. The `total` field is unreliable (jumps backwards),
    # so we only use the delta for the display counter.
    stats = like_tracker.process(data)
    delta = stats.event_like_count
    if delta <= 0:
        # Spurious event with no new likes — still capture the liker name.
        liker_uid = data.get("user", {}).get("uniqueId")
        if liker_uid:
            display_names[liker_uid] = data.get("user", {}).get("nickname") or liker_uid
        return
    display_like_total += delta
    like_total = stats.total_like_count
    # Capture WHO liked (TikTok exposes the liker's uniqueId in the event).
    # Show the display name (nickname), keyed by uniqueId so renames don't split.
    liker_uid = data.get("user", {}).get("uniqueId")
    if liker_uid:
        liker_name = data.get("user", {}).get("nickname") or liker_uid
        display_names[liker_uid] = liker_name
        # De-dupe by display name, then prepend (most recent first)
        recent_likers = [u for u in recent_likers if u != liker_name][:9]
        recent_likers.insert(0, liker_name)
    # Each tap in the burst feeds the rolling hype window (rate-based).
    # UNCAPPED — a massive tap storm scales linearly with its size.
    like_weight = delta * HYPE_LIKE_WEIGHT
    _hype_add(like_weight)
    asyncio.create_task(broadcast(snapshot()))


def on_follow(evt):
    global follow_count
    _touch()
    follow_count += 1
    user = evt.data.get("user", {}).get("uniqueId", "?")
    print(f"[FOLLOW] @{user} followed (total {follow_count})")
    asyncio.create_task(broadcast(snapshot()))


async def hype_decay_loop():
    """Hype hold + countdown + drain (layered on the rate-based target).

    While engagement flows, _hype_add() keeps pushing hype_level up to the
    live window target and cancels any drain. Once likes/gifts stop:

      Phase 1 (GRACE, HYPE_GRACE_SECS): hold the meter steady — no drop.
      Phase 2 (COUNTDOWN, HYPE_COUNTDOWN_SECS): run a visible 15s ticker; the
        meter still holds at its current level (UI shows the countdown).
      Phase 3 (DRAIN): countdown finished — ease the meter down toward the
        window target (which has been falling as old events age out). Any new
        like/gift during any phase resets back to grace.
    """
    global hype_level, hype_target, hype_countdown_active, hype_countdown_start
    while True:
        await asyncio.sleep(1)
        if hype_level <= 0 or last_activity_time == 0:
            hype_countdown_active = False
            continue
        now = asyncio.get_event_loop().time()
        quiet = now - last_activity_time

        if not hype_countdown_active:
            # GRACE phase — hold steady until the quiet window exceeds grace.
            if quiet >= HYPE_GRACE_SECS:
                hype_countdown_active = True
                hype_countdown_start = now
                print(f"[HYPE-COUNTDOWN] started — draining in {HYPE_COUNTDOWN_SECS}s (level {hype_level})", flush=True)
                await broadcast(snapshot())
            continue

        # COUNTDOWN phase — hold the meter, let the UI tick down.
        elapsed = quiet - HYPE_GRACE_SECS
        if elapsed < HYPE_COUNTDOWN_SECS:
            continue  # still counting; snapshot() computes the live ticker

        # COUNTDOWN finished — DRAIN toward the window target.
        hype_target = _hype_window_level()
        if hype_target < hype_level:
            # Ease down by at most a few steps per tick for a smooth slide.
            new_level = max(hype_target, hype_level - 2)
            if new_level != hype_level:
                hype_level = new_level
                print(f"[HYPE-DECAY] drained to level {hype_level} (x{current_multiplier()})", flush=True)
                await broadcast(snapshot())
        else:
            # Engagement resumed (target back up) — stop draining, hold.
            hype_countdown_active = False


async def stale_watchdog(client: TikTokLiveClient):
    """If the TikTok connection reports connected but no events arrive for
    STALE_RECONNECT_SECS, force a disconnect so run_tiktok() reconnects and
    (usually) gets a fresh data stream that carries likes/gifts again."""
    global last_event_time
    while True:
        await asyncio.sleep(5)
        if not connected or last_event_time == 0:
            continue
        now = asyncio.get_event_loop().time()
        if now - last_event_time >= STALE_RECONNECT_SECS:
            # Connection has been silent for a VERY long time. Only force a
            # disconnect if TikTok hasn't already dropped us — otherwise we'd
            # tear down a healthy live stream that's just between event bursts.
            if connected:
                print(f"[STALE] No events for {STALE_RECONNECT_SECS}s but still connected — waiting (live stream may just be quiet)", flush=True)
            else:
                print(f"[STALE] No events for {STALE_RECONNECT_SECS}s and disconnected — forcing reconnect", flush=True)
                try:
                    client.disconnect()
                except Exception as e:
                    print(f"[STALE] disconnect error: {e}", flush=True)


# ---- HTTP + WebSocket server (same port, same origin) ----
import os
import http
from http.server import SimpleHTTPRequestHandler

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PORT = 8766  # HTTP page AND WebSocket both served here (same origin)


class StaticHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def log_message(self, *args):
        pass  # silence HTTP logs

    def end_headers(self):
        # Allow the WebSocket upgrade from the same origin (no CORS issues)
        self.send_header("Access-Control-Allow-Origin", "*")
        # NEVER cache — TikTok Live Studio / OBS browser sources aggressively
        # cache static files, leaving the overlay stuck on a stale app.js
        # (e.g. showing like_total=0 because the cached JS predates the fix).
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


def make_http_handler():
    """Return a process_request callable for websockets.serve that serves
    static files for normal HTTP requests and lets WS upgrades through.

    websockets v16 signature: process_request(self, request) where request
    is a websockets.asyncio.connection.Request with .path (str) and
    .headers (multidict-like). Returning None lets the WS handshake proceed.
    Returning a Response object sends an HTTP response instead.
    """
    from websockets.asyncio.connection import Response
    from websockets.legacy.http import Headers

    def process_request(self, request):
        method = (request.headers.get("Method") or request.headers.get(":method")
                  or getattr(request, "method", "GET") or "GET")
        method = method.upper()

        # Let websockets handle the WS upgrade handshake.
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return

        # CORS preflight (OPTIONS) — answer it cleanly so the server
        # never chokes on a non-GET request. Returning a Response object
        # stops websockets from trying to parse it as a WS handshake.
        if method == "OPTIONS":
            from websockets.asyncio.connection import Response
            from websockets.legacy.http import Headers
            headers = Headers([
                ("Access-Control-Allow-Origin", "*"),
                ("Access-Control-Allow-Methods", "GET, OPTIONS"),
                ("Access-Control-Allow-Headers", "*"),
                ("Content-Length", "0"),
            ])
            return Response(204, "No Content", headers, b"")

        # Only GET is served beyond this point. Anything else -> 405.
        if method != "GET":
            from websockets.asyncio.connection import Response
            from websockets.legacy.http import Headers
            headers = Headers([
                ("Access-Control-Allow-Origin", "*"),
                ("Content-Type", "text/plain"),
                ("Content-Length", "11"),
            ])
            return Response(405, "Method Not Allowed", headers, b"Method not allowed")

        # Live state as JSON — lets the overlay work even if the WebSocket
        # is blocked by the OBS/TikTok Live Studio browser sandbox. The page
        # polls this endpoint as a fallback so likes/gifts always update.
        path = request.path.split("?")[0]
        if path == "/state":
            from websockets.asyncio.connection import Response
            from websockets.legacy.http import Headers
            body = json.dumps(snapshot()).encode("utf-8")
            headers = Headers([
                ("Access-Control-Allow-Origin", "*"),
                ("Content-Type", "application/json"),
                ("Cache-Control", "no-store"),
                ("Content-Length", str(len(body))),
            ])
            return Response(200, "OK", headers, body)

        # Serve the requested file (or index.html for "/")
        if path in ("/", ""):
            path = "/index.html"
        filepath = os.path.join(STATIC_DIR, os.path.basename(path))
        try:
            with open(filepath, "rb") as f:
                body = f.read()
            if filepath.endswith(".html"):
                ctype = "text/html"
            elif filepath.endswith(".js"):
                ctype = "application/javascript"
            elif filepath.endswith(".css"):
                ctype = "text/css"
            else:
                ctype = "application/octet-stream"
            status = 200
            reason = "OK"
        except FileNotFoundError:
            body = b"Not found"
            ctype = "text/plain"
            status = 404
            reason = "Not Found"

        from websockets.asyncio.connection import Response
        from websockets.legacy.http import Headers
        headers = Headers([
            ("Content-Type", ctype),
            ("Content-Length", str(len(body))),
            ("Access-Control-Allow-Origin", "*"),
            # NEVER cache static files — OBS / TikTok Live Studio browser
            # sources cache app.js/index.css aggressively, which leaves the
            # overlay stuck on a stale build (e.g. like_total frozen at 0).
            ("Cache-Control", "no-store, no-cache, must-revalidate"),
            ("Pragma", "no-cache"),
            ("Expires", "0"),
        ])
        return Response(status, reason, headers, body)

    return process_request


async def ws_handler(websocket):
    CLIENTS.add(websocket)
    try:
        await websocket.send(json.dumps(snapshot()))
        async for _ in websocket:
            pass  # we only push; ignore client messages
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        CLIENTS.discard(websocket)


async def run_tiktok(client: TikTokLiveClient, username: str):
    """Connect to TikTok with auto-reconnect. Never lets the server die."""
    while True:
        try:
            print(f"[START] Connecting to @{username} ...")
            await client.connect()
            # client.connect() returns when the stream ends; loop to reconnect
            print("[INFO] Stream ended. Reconnecting in 5s...")
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[WARN] TikTok connection failed: {e}")
            print("[INFO] HTTP + WebSocket server still running. Retrying in 5s...")
        await asyncio.sleep(5)


async def main(username: str):
    global stream_user
    stream_user = username
    clean_user = username.lstrip("@")
    # PirateTok needs no API key or sign server — connect directly.
    client = TikTokLiveClient(clean_user)

    # Register handlers
    client.on(EventType.connected)(on_connect)
    client.on(EventType.disconnected)(on_disconnect)
    client.on(EventType.gift)(on_gift)
    client.on(EventType.like)(on_like)
    client.on(EventType.follow)(on_follow)

    # Start combined HTTP + WebSocket server on a SINGLE port (same origin).
    # This avoids TikTok Live Studio's browser sandbox blocking cross-port
    # or cross-origin WebSocket connections.
    ws_server = await websockets.serve(
        ws_handler,
        "0.0.0.0",
        PORT,
        process_request=make_http_handler(),
    )
    print(f"[SERVER] Leaderboard + WebSocket on http://0.0.0.0:{PORT}/")
    print(f"[SERVER] In TikTok Live Studio use: http://<YOUR_LAN_IP>:{PORT}/")
    print(f"[SERVER] WebSocket auto-connects on ws://<same-host>:{PORT}/ (same origin)")

    # Run TikTokLive client in a background task (auto-reconnect loop).
    # The HTTP + WS servers stay alive no matter what happens to TikTok.
    print(f"[START] Launching TikTok listener for @{clean_user}")
    tiktok_task = asyncio.create_task(run_tiktok(client, clean_user))
    # Hype decay watchdog: drops hype if likes stall.
    decay_task = asyncio.create_task(hype_decay_loop())
    # Stale watchdog: force reconnect if connected but no events flow.
    stale_task = asyncio.create_task(stale_watchdog(client))

    # Keep the servers alive forever (until SIGINT)
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        tiktok_task.cancel()
        ws_server.close()
        await ws_server.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TikTok Live Leaderboard Server")
    parser.add_argument("--user", required=True, help="TikTok username (with or without @)")
    args = parser.parse_args()
    asyncio.run(main(args.user))
