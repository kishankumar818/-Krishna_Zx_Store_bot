import json
import os
import socketserver
import threading
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

IST = timezone(timedelta(hours=5, minutes=30))

BOT_STATE = {
    "status": "starting",
    "bot_username": os.environ.get("BOT_USERNAME", "@Krishna_Zx_Store_bot"),
    "store_name": os.environ.get("STORE_NAME", "Krishna_Zx_Store"),
    "started_at": time.time(),
    " started_str": datetime.now(IST).strftime("%d-%m-%Y %I:%M:%S %p IST"),
    "last_heartbeat": time.time(),
    "last_heartbeat_str": datetime.now(IST).strftime("%d-%m-%Y %I:%M:%S %p IST"),
    "last_ping": None,
    "ping_count": 0,
    "restart_count": 0,
    "last_error": "",
    "port": int(os.environ.get("PORT", "3000")),
}

_SERVER_STARTED = False
_LOCK = threading.Lock()


def update_bot_state(**kwargs):
    with _LOCK:
        BOT_STATE.update(kwargs)
        if "last_heartbeat" in kwargs or kwargs.get("status") == "running":
            BOT_STATE["last_heartbeat"] = time.time()
            BOT_STATE["last_heartbeat_str"] = datetime.now(IST).strftime("%d-%m-%Y %I:%M:%S %p IST")


def _format_uptime(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {mins}m {secs}s"
    if hours > 0:
        return f"{hours}h {mins}m {secs}s"
    return f"{mins}m {secs}s"


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    def _send_headers(self, code=200, content_type="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def do_HEAD(self):
        # UptimeRobot aur dusre monitors default mein HEAD request bhejte hain.
        # Isko 200 OK dena zaroori hai taaki bot kabhi offline mark na ho.
        with _LOCK:
            BOT_STATE["ping_count"] += 1
            BOT_STATE["last_ping"] = datetime.now(IST).strftime("%d-%m-%Y %I:%M:%S %p IST")
        self._send_headers(200, "text/plain; charset=utf-8")

    def do_GET(self):
        with _LOCK:
            BOT_STATE["ping_count"] += 1
            BOT_STATE["last_ping"] = datetime.now(IST).strftime("%d-%m-%Y %I:%M:%S %p IST")
            state_copy = dict(BOT_STATE)

        uptime_sec = time.time() - state_copy["started_at"]
        uptime_str = _format_uptime(uptime_sec)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path in ("/ping", "/health", "/healthz", "/alive"):
            self._send_headers(200, "text/plain; charset=utf-8")
            msg = f"OK - {state_copy['store_name']} Bot is alive 24/7 | Uptime: {uptime_str}\n"
            self.wfile.write(msg.encode("utf-8"))
            return

        if path in ("/status", "/api/health", "/api/status"):
            self._send_headers(200, "application/json; charset=utf-8")
            payload = {
                "ok": True,
                "status": state_copy["status"],
                "store": state_copy["store_name"],
                "bot": state_copy["bot_username"],
                "uptime_seconds": int(uptime_sec),
                "uptime": uptime_str,
                "started_at": state_copy[" started_str"],
                "last_heartbeat": state_copy["last_heartbeat_str"],
                "last_ping": state_copy["last_ping"],
                "ping_count": state_copy["ping_count"],
                "restart_count": state_copy["restart_count"],
                "last_error": state_copy["last_error"],
            }
            self.wfile.write(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
            return

        if path == "/":
            accept = (self.headers.get("Accept") or "").lower()
            if "text/html" in accept:
                self._send_headers(200, "text/html; charset=utf-8")
                status_val = state_copy["status"]
                badge_color = "#10b981" if status_val == "running" else "#f59e0b"
                badge_text = "ONLINE 24/7" if status_val == "running" else status_val.upper()
                error_block = ""
                if state_copy.get("last_error"):
                    error_block = (
                        f'<div style="margin-top:16px;padding:12px 16px;background:rgba(239,68,68,0.12);'
                        f'border:1px solid rgba(239,68,68,0.35);border-radius:10px;color:#fca5a5;font-size:13px;">'
                        f'<strong>Notice:</strong> {state_copy["last_error"]}</div>'
                    )
                html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta http-equiv="refresh" content="30" />
  <title>{state_copy['store_name']} — 24/7 Bot Keep-Alive Monitor</title>
  <meta name="description" content="24/7 Keep-Alive & Auto-Recovery Monitor for {state_copy['store_name']} Telegram Bot." />
  <meta property="og:title" content="{state_copy['store_name']} — 24/7 Bot Monitor" />
  <meta property="og:description" content="24/7 Keep-Alive & Auto-Recovery Monitor for {state_copy['store_name']} Telegram Bot." />
  <style>
    :root {{
      --bg: #0b0f19;
      --card: #111827;
      --border: #1f2937;
      --text: #f9fafb;
      --muted: #9ca3af;
      --accent: #10b981;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }}
    .container {{
      width: 100%;
      max-width: 720px;
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 28px;
      box-shadow: 0 20px 50px rgba(0,0,0,0.45);
    }}
    .header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border);
      flex-wrap: wrap;
    }}
    .title h1 {{ font-size: 22px; font-weight: 700; letter-spacing: -0.02em; }}
    .title p {{ color: var(--muted); font-size: 14px; margin-top: 4px; }}
    .badge {{
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 6px 14px;
      border-radius: 6px;
      font-size: 13px;
      font-weight: 600;
      background: rgba(16, 185, 129, 0.15);
      color: {badge_color};
      border: 1px solid {badge_color};
    }}
    .dot {{
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: {badge_color};
      box-shadow: 0 0 10px {badge_color};
    }}
    .grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 14px;
      margin-top: 20px;
    }}
    .stat {{
      background: rgba(255,255,255,0.02);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 14px 16px;
    }}
    .stat-label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: 0.05em; }}
    .stat-val {{ font-size: 18px; font-weight: 600; margin-top: 6px; font-variant-numeric: tabular-nums; }}
    .guide {{
      margin-top: 22px;
      padding: 16px;
      background: rgba(59, 130, 246, 0.08);
      border: 1px solid rgba(59, 130, 246, 0.25);
      border-radius: 12px;
      font-size: 13.5px;
      line-height: 1.6;
      color: #dbeafe;
    }}
    .guide code {{
      background: rgba(0,0,0,0.4);
      padding: 2px 6px;
      border-radius: 4px;
      color: #93c5fd;
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }}
  </style>
</head>
<body>
  <main class="container">
    <div class="header">
      <div class="title">
        <h1>{state_copy['store_name']}</h1>
        <p>Telegram Bot: {state_copy['bot_username']} • 24/7 Keep-Alive & Auto-Restart Active</p>
      </div>
      <div class="badge"><span class="dot"></span> {badge_text}</div>
    </div>

    <div class="grid">
      <div class="stat">
        <div class="stat-label">Continuous Uptime</div>
        <div class="stat-val">{uptime_str}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Keep-Alive Pings</div>
        <div class="stat-val">{state_copy['ping_count']}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Auto-Recoveries</div>
        <div class="stat-val">{state_copy['restart_count']}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Last Bot Heartbeat</div>
        <div class="stat-val" style="font-size:13px;">{state_copy['last_heartbeat_str']}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Last HTTP Ping</div>
        <div class="stat-val" style="font-size:13px;">{state_copy['last_ping'] or 'Just now'}</div>
      </div>
      <div class="stat">
        <div class="stat-label">Started At</div>
        <div class="stat-val" style="font-size:13px;">{state_copy[' started_str']}</div>
      </div>
    </div>

    {error_block}

    <div class="guide">
      <strong>24/7 Online Guarantee Setup:</strong><br/>
      1. Built-in <strong>Self-Pinger</strong> aur <strong>Auto-Restart Supervisor</strong> chalu hai (har 4 minute mein self-ping + crash/network error par 5 sec mein auto-reconnect).<br/>
      2. Render / Hosting ke Environment Variables mein <code>RENDER_EXTERNAL_URL</code> ya <code>KEEP_ALIVE_URL</code> set karein, ya <strong>UptimeRobot / Cron-job.org</strong> mein apna URL <code>/ping</code> ya <code>/health</code> (GET &amp; HEAD dono supported) 5 minute interval par lagayein.
    </div>
  </main>
</body>
</html>"""
                self.wfile.write(html.encode("utf-8"))
                return

            self._send_headers(200, "text/plain; charset=utf-8")
            self.wfile.write(f"OK - {state_copy['store_name']} Bot is alive 24/7 (Uptime: {uptime_str})".encode("utf-8"))
            return

        self._send_headers(404, "text/plain; charset=utf-8")
        self.wfile.write(b"Not Found")

    def log_message(self, *args):
        pass  # silent HTTP logs


def _get_self_ping_urls(port: int) -> list[str]:
    urls = []
    for env_key in ("KEEP_ALIVE_URL", "RENDER_EXTERNAL_URL", "APP_URL", "PUBLIC_URL", "SPACE_HOST"):
        val = os.environ.get(env_key, "").strip()
        if val:
            if not val.startswith(("http://", "https://")):
                val = "https://" + val
            ping_url = val.rstrip("/") + "/ping"
            if ping_url not in urls:
                urls.append(ping_url)
    local_url = f"http://127.0.0.1:{port}/ping"
    if local_url not in urls:
        urls.append(local_url)
    return urls


def _self_ping_loop(port: int):
    """Har 4 minute (240s) mein server ko ping karta hai taaki free hosting sleep na ho."""
    time.sleep(15)
    interval = int(os.environ.get("KEEP_ALIVE_INTERVAL", "240"))
    while True:
        for url in _get_self_ping_urls(port):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "KrishnaStore-KeepAlive/2.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    _ = resp.read(64)
            except Exception:
                pass
        time.sleep(max(30, interval))


def start_keep_alive() -> int:
    global _SERVER_STARTED
    port = int(os.environ.get("PORT", "3000"))
    with _LOCK:
        BOT_STATE["port"] = port
        if _SERVER_STARTED:
            return port
        _SERVER_STARTED = True

    try:
        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        thread = threading.Thread(target=server.serve_forever, name="KeepAliveHTTP", daemon=True)
        thread.start()
        print(f"✅ 24/7 Keep-alive HTTP server listening on 0.0.0.0:{port}", flush=True)
    except OSError as e:
        print(f"⚠️ Keep-alive server port {port} bind warning: {e}", flush=True)

    ping_thread = threading.Thread(target=_self_ping_loop, args=(port,), name="SelfPinger", daemon=True)
    ping_thread.start()
    return port
