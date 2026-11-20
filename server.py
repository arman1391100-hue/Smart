#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""سرور کوچک بازی «کی باهوش‌تره؟» به زبان پایتون — فقط با کتابخانه‌ی استاندارد (بدون نصب چیزی).

اجرا:   python server.py
پورت:   متغیر محیطی PORT (پیش‌فرض ۳۰۰۰)
فایل‌ها: quiz.html کنار همین فایل و (اختیاری) پوشه‌ی music
"""
import hashlib
import hmac
import ipaddress
import json
import os
import queue
import re
import select
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("PORT", 3000))
HTML_FILE = os.path.join(BASE_DIR, "quiz.html")
MUSIC_DIR = os.path.join(BASE_DIR, "music")
AUDIO = {
    ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".oga": "audio/ogg", ".opus": "audio/ogg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav", ".webm": "audio/webm",
    ".flac": "audio/flac",
}

SECRET = os.urandom(16)
GRACE_S = 60            # اگر اتصال قطع شد، تا ۶۰ ثانیه جای بازیکن در اتاق می‌ماند
KEEPALIVE_S = 20
MAX_CLIENTS, MAX_ROOMS, MAX_MEMBERS, MAX_BODY = 5000, 3000, 500, 200 * 1024

LOCK = threading.RLock()
clients = {}      # id -> Client
rooms = {}        # roomName -> set(id)
last_state = {}   # roomName -> آخرین پیام state


class Client:
    def __init__(self):
        self.q = None          # صف اتصال فعلی (None یعنی اتصال قطع است)
        self.rooms = {}        # roomName -> presence dict
        self.timer = None


# ---------------------------------------------------------------- کلید «نزدیک»
def ip_group(ip):
    """IPv4 یکسان یا IPv6 با پیشوند /64 یکسان => گروه یکسان."""
    ip = str(ip or "").strip()
    ip = re.sub(r"^::ffff:", "", ip, flags=re.I)
    try:
        addr = ipaddress.ip_address(ip)
        if addr.version == 6:
            return str(ipaddress.ip_network(ip + "/64", strict=False).network_address)
        return str(addr)
    except ValueError:
        return ip


def near_key(handler):
    xf = (handler.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    ip = xf or handler.client_address[0] or ""
    return hmac.new(SECRET, ip_group(ip).encode(), hashlib.sha256).hexdigest()[:12]


# ---------------------------------------------------------------- منطق اتاق‌ها
def write(c, obj):
    q = c.q
    if q is not None:
        q.put(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def peers_of(room):
    out = []
    for pid in rooms.get(room, ()):
        c = clients.get(pid)
        out.append({"peer": pid, "pr": (c.rooms.get(room) if c else None) or {}})
    return out


def broadcast_peers(room):
    s = rooms.get(room)
    if not s:
        return
    msg = {"t": "peers", "room": room, "peers": peers_of(room)}
    for pid in list(s):
        c = clients.get(pid)
        if c:
            write(c, msg)


def join(cid, room):
    c = clients.get(cid)
    if not c:
        return
    if room not in rooms:
        if len(rooms) >= MAX_ROOMS:
            return
        rooms[room] = set()
    s = rooms[room]
    if cid not in s and len(s) >= MAX_MEMBERS:
        return
    s.add(cid)
    c.rooms.setdefault(room, {})
    broadcast_peers(room)
    if room in last_state:
        write(c, last_state[room])


def leave(cid, room):
    c = clients.get(cid)
    s = rooms.get(room)
    if c:
        c.rooms.pop(room, None)
    if s is not None:
        s.discard(cid)
        if not s:
            rooms.pop(room, None)
            last_state.pop(room, None)
        else:
            broadcast_peers(room)


def emit(cid, room, topic, data):
    s = rooms.get(room)
    if not s or cid not in s:
        return
    msg = {"t": "msg", "room": room, "topic": topic, "data": data, "peer": cid}
    if topic == "state":
        last_state[room] = msg
    for pid in list(s):
        c = clients.get(pid)
        if c:
            write(c, msg)


def presence(cid, room, patch):
    c = clients.get(cid)
    if not c or room not in c.rooms or not isinstance(patch, dict):
        return
    c.rooms[room].update(patch)
    broadcast_peers(room)


def drop_client(cid, my_q):
    """بعد از پایان مهلت، اگر هنوز اتصال جدیدی نیامده بازیکن را حذف کن."""
    with LOCK:
        c = clients.get(cid)
        if not c or c.q is not None or c.timer is None:
            return
        for room in list(c.rooms.keys()):
            leave(cid, room)
        clients.pop(cid, None)


def js_str(v, limit):
    """معادل String(v).slice(0, limit) در جاوااسکریپت (برای null و undefined هم)."""
    if v is None:
        v = ""
    if isinstance(v, bool):
        v = "true" if v else "false"
    return str(v)[:limit]


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 120
    server_version = "QuizServer"

    def log_message(self, *args):   # لاگ پرنویز نشود
        pass

    # ---- ابزارها
    def _send(self, code, body=b"", ctype=None, extra=None):
        self.send_response(code)
        if ctype:
            self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    # ---- GET
    def do_GET(self):
        url = urlsplit(self.path)
        path = url.path
        try:
            if path in ("/", "/index.html"):
                return self._html()
            if path == "/music-list":
                return self._music_list()
            if path.startswith("/music/"):
                return self._music(path[7:])
            if path == "/ping":
                return self._send(200, b'{"qk":true}', "application/json", {"Cache-Control": "no-store"})
            if path == "/sse":
                return self._sse(parse_qs(url.query))
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True

    do_HEAD = do_GET

    def _html(self):
        try:
            with open(HTML_FILE, "rb") as f:
                buf = f.read()
        except OSError:
            return self._send(500, b"quiz.html not found", "text/plain; charset=utf-8")
        self._send(200, buf, "text/html; charset=utf-8", {"Cache-Control": "no-cache"})

    def _music_list(self):
        try:
            files = sorted(f for f in os.listdir(MUSIC_DIR) if os.path.splitext(f)[1].lower() in AUDIO)
        except OSError:
            files = []
        self._send(200, json.dumps(files, ensure_ascii=False).encode(), "application/json", {"Cache-Control": "no-store"})

    def _music(self, raw):
        try:
            name = os.path.basename(unquote(raw))
        except Exception:
            return self._send(400)
        ctype = AUDIO.get(os.path.splitext(name)[1].lower())
        if not ctype:
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        fp = os.path.join(MUSIC_DIR, name)
        if not os.path.isfile(fp):
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        size = os.path.getsize(fp)
        m = re.match(r"^bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            start = int(m.group(1)) if m.group(1) else max(0, size - int(m.group(2)))
            end = min(int(m.group(2)), size - 1) if (m.group(1) and m.group(2)) else size - 1
            if start > end or start >= size:
                return self._send(416, b"", None, {"Content-Range": "bytes */%d" % size})
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.send_header("Content-Length", str(end - start + 1))
        else:
            start, end = 0, size - 1
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(fp, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                self.wfile.write(chunk)
                left -= len(chunk)

    def _sse(self, qs):
        pid = (qs.get("pid") or [""])[0]
        valid = re.fullmatch(r"[a-z0-9]{8,24}", pid or "") is not None
        cid = pid if valid else os.urandom(6).hex()
        near = near_key(self)
        my_q = queue.Queue()
        with LOCK:
            c = clients.get(cid)
            resumed = False
            if c is None and len(clients) >= MAX_CLIENTS:
                return self._send(503)
            if c is not None:       # همان بازیکنی که اتصالش قطع شده بود دوباره وصل شد
                if c.timer:
                    c.timer.cancel()
                    c.timer = None
                if c.q is not None:
                    c.q.put(None)   # اتصال قدیمی را ببند
                resumed = True
            else:
                c = Client()
                clients[cid] = c
            c.q = my_q
            write(c, {"t": "hello", "id": cid, "near": near, "resumed": resumed})
            if resumed:
                for room in list(c.rooms.keys()):
                    write(c, {"t": "peers", "room": room, "peers": peers_of(room)})
                    if room in last_state:
                        write(c, last_state[room])

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        self.wfile.flush()

        last_beat = 0.0
        try:
            while True:
                try:
                    item = my_q.get(timeout=1)
                except queue.Empty:
                    item = ""
                if item is None:
                    break
                if item:
                    self.wfile.write(("data: " + item + "\n\n").encode("utf-8"))
                    self.wfile.flush()
                    continue
                now = time.time()
                if now - last_beat >= KEEPALIVE_S:
                    last_beat = now
                    self.wfile.write(b": ka\n\n")
                    self.wfile.flush()
                # قطع‌شدن سمت مرورگر را سریع تشخیص بده
                r, _, _ = select.select([self.connection], [], [], 0)
                if r:
                    try:
                        if self.connection.recv(1, 0x02) == b"":   # MSG_PEEK
                            break
                    except (BlockingIOError, InterruptedError):
                        pass
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            with LOCK:
                cur = clients.get(cid)
                if cur is not None and cur.q is my_q:    # اتصال جدیدتری جایگزین نشده است
                    cur.q = None
                    t = threading.Timer(GRACE_S, drop_client, args=(cid, my_q))
                    t.daemon = True
                    cur.timer = t
                    t.start()

    # ---- POST
    def do_POST(self):
        path = urlsplit(self.path).path
        if path != "/send":
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            n = -1
        if n < 0:
            self.close_connection = True
            return self._send(400)
        if n > MAX_BODY:
            self.close_connection = True
            return self._send(413)
        body = self.rfile.read(n) if n else b""
        try:
            b = json.loads(body.decode("utf-8"))
            if not isinstance(b, dict):
                raise ValueError
            cid = b.get("id")
            room = js_str(b.get("room"), 64)
            op = b.get("op")
            with LOCK:
                if isinstance(cid, str) and cid in clients:
                    if op == "join":
                        join(cid, room)
                    elif op == "leave":
                        leave(cid, room)
                    elif op == "emit":
                        emit(cid, room, js_str(b.get("topic"), 32), b.get("data"))
                    elif op == "presence":
                        presence(cid, room, b.get("patch"))
            self._send(204)
        except (ValueError, UnicodeDecodeError):
            self._send(400)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256
    allow_reuse_address = True


def main():
    srv = Server(("0.0.0.0", PORT), Handler)
    print("quiz server on port %d" % PORT, flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
