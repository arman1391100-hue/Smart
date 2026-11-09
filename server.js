// سرور کوچک بازی «کی باهوش‌تره؟» — بدون هیچ وابستگی اضافه
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const PORT = process.env.PORT || 3000;
const HTML_FILE = path.join(__dirname, 'quiz.html');

const SECRET = crypto.randomBytes(16);

// کلید «نزدیک»: دستگاه‌هایی که از یک اینترنت می‌آیند (IPv4 یکسان یا IPv6 با پیشوند /64 یکسان) کلید یکسان می‌گیرند.
// کلید یک هش بدون برگشت است و آدرس IP هیچ‌جا به کاربران نشان داده نمی‌شود.
function ipGroup(ip) {
  ip = String(ip || '').trim().replace(/^::ffff:/i, '');
  if (ip.includes(':')) {
    const parts = ip.split('::');
    const a = parts[0] ? parts[0].split(':') : [];
    const b = parts.length > 1 && parts[1] ? parts[1].split(':') : [];
    if (parts.length > 1) while (a.length + b.length < 8) b.unshift('0');
    return a.concat(b).slice(0, 4).map(x => x.toLowerCase().replace(/^0+(?=.)/, '')).join(':');
  }
  return ip;
}
function nearKey(req) {
  const xf = String(req.headers['x-forwarded-for'] || '').split(',')[0].trim();
  const ip = xf || req.socket.remoteAddress || '';
  return crypto.createHmac('sha256', SECRET).update(ipGroup(ip)).digest('hex').slice(0, 12);
}

const clients = new Map(); // id -> { res, rooms: Map(roomName -> presenceObject), timer }
const lastState = new Map(); // roomName -> آخرین پیام state (برای کسی که دیر وارد می‌شود یا دوباره وصل می‌شود)
const GRACE_MS = 60000; // اگر اتصال قطع شد، تا ۶۰ ثانیه جای بازیکن در اتاق می‌ماند
const rooms = new Map();   // roomName -> Set(id)

const MAX_CLIENTS = 5000, MAX_ROOMS = 3000, MAX_MEMBERS = 500, MAX_BODY = 200 * 1024;

const write = (c, obj) => { try { if (c.res && !c.res.writableEnded) c.res.write('data: ' + JSON.stringify(obj) + '\n\n'); } catch (e) {} };

function peersOf(room) {
  const set = rooms.get(room);
  if (!set) return [];
  return [...set].map(id => ({ peer: id, pr: (clients.get(id)?.rooms.get(room)) || {} }));
}
function broadcastPeers(room) {
  const set = rooms.get(room);
  if (!set) return;
  const msg = { t: 'peers', room, peers: peersOf(room) };
  for (const id of set) { const c = clients.get(id); if (c) write(c, msg); }
}
function join(id, room) {
  const c = clients.get(id);
  if (!c) return;
  if (!rooms.has(room)) {
    if (rooms.size >= MAX_ROOMS) return;
    rooms.set(room, new Set());
  }
  const set = rooms.get(room);
  if (!set.has(id) && set.size >= MAX_MEMBERS) return;
  set.add(id);
  if (!c.rooms.has(room)) c.rooms.set(room, {});
  broadcastPeers(room);
  if (lastState.has(room)) write(c, lastState.get(room));
}
function leave(id, room) {
  const c = clients.get(id);
  const set = rooms.get(room);
  if (c) c.rooms.delete(room);
  if (set) {
    set.delete(id);
    if (set.size === 0) { rooms.delete(room); lastState.delete(room); }
    else broadcastPeers(room);
  }
}
function emit(id, room, topic, data) {
  const set = rooms.get(room);
  if (!set || !set.has(id)) return;
  const msg = { t: 'msg', room, topic, data, peer: id };
  if (topic === 'state') lastState.set(room, msg);
  for (const pid of set) { const c = clients.get(pid); if (c) write(c, msg); }
}
function presence(id, room, patch) {
  const c = clients.get(id);
  if (!c || !c.rooms.has(room) || typeof patch !== 'object' || !patch) return;
  c.rooms.set(room, Object.assign(c.rooms.get(room), patch));
  broadcastPeers(room);
}

const server = http.createServer((req, res) => {
  const url = req.url.split('?')[0];

  if (req.method === 'GET' && (url === '/' || url === '/index.html')) {
    fs.readFile(HTML_FILE, (err, buf) => {
      if (err) { res.writeHead(500); return res.end('quiz.html not found'); }
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-cache' });
      res.end(buf);
    });
    return;
  }

  if (req.method === 'GET' && url === '/ping') {
    res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
    return res.end(JSON.stringify({ qk: true }));
  }

  if (req.method === 'GET' && url === '/sse') {
    const pidq = new URL(req.url, 'http://x').searchParams.get('pid') || '';
    const valid = /^[a-z0-9]{8,24}$/.test(pidq);
    const id = valid ? pidq : crypto.randomBytes(6).toString('hex');
    let c = clients.get(id), resumed = false;
    if (!c && clients.size >= MAX_CLIENTS) { res.writeHead(503); return res.end(); }
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no'
    });
    if (c) { // همان بازیکنی که اتصالش قطع شده بود دوباره وصل شد: اتاق‌هایش را حفظ کن
      clearTimeout(c.timer);
      try { c.res.end(); } catch (e) {}
      c.res = res; resumed = true;
    } else {
      c = { res, rooms: new Map(), timer: null };
      clients.set(id, c);
    }
    write(c, { t: 'hello', id, near: nearKey(req), resumed });
    if (resumed) {
      for (const room of c.rooms.keys()) {
        write(c, { t: 'peers', room, peers: peersOf(room) });
        if (lastState.has(room)) write(c, lastState.get(room));
      }
    }
    const ka = setInterval(() => { try { res.write(': ka\n\n'); } catch (e) {} }, 20000);
    req.on('close', () => {
      clearInterval(ka);
      if (c.res !== res) return; // اتصال جدیدتری جایگزین شده است
      c.timer = setTimeout(() => {
        if (c.res !== res) return;
        for (const room of [...c.rooms.keys()]) leave(id, room);
        clients.delete(id);
      }, GRACE_MS);
    });
    return;
  }

  if (req.method === 'POST' && url === '/send') {
    let body = '', size = 0, dead = false;
    req.on('data', ch => {
      size += ch.length;
      if (size > MAX_BODY) { dead = true; res.writeHead(413); res.end(); req.destroy(); return; }
      body += ch;
    });
    req.on('end', () => {
      if (dead) return;
      try {
        const b = JSON.parse(body);
        const id = b.id, room = String(b.room == null ? '' : b.room).slice(0, 64);
        if (clients.has(id)) {
          if (b.op === 'join') join(id, room);
          else if (b.op === 'leave') leave(id, room);
          else if (b.op === 'emit') emit(id, room, String(b.topic).slice(0, 32), b.data);
          else if (b.op === 'presence') presence(id, room, b.patch);
        }
        res.writeHead(204); res.end();
      } catch (e) { res.writeHead(400); res.end(); }
    });
    return;
  }

  res.writeHead(404); res.end('not found');
});

server.listen(PORT, () => console.log('quiz server on port ' + PORT));
