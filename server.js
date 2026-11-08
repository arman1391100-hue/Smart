// سرور کوچک بازی «کی باهوش‌تره؟» — بدون هیچ وابستگی اضافه
const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const PORT = process.env.PORT || 3000;
const HTML_FILE = path.join(__dirname, 'quiz.html');

const clients = new Map(); // id -> { res, rooms: Map(roomName -> presenceObject) }
const rooms = new Map();   // roomName -> Set(id)

const MAX_CLIENTS = 5000, MAX_ROOMS = 3000, MAX_MEMBERS = 500, MAX_BODY = 200 * 1024;

const write = (c, obj) => { try { c.res.write('data: ' + JSON.stringify(obj) + '\n\n'); } catch (e) {} };

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
}
function leave(id, room) {
  const c = clients.get(id);
  const set = rooms.get(room);
  if (c) c.rooms.delete(room);
  if (set) {
    set.delete(id);
    if (set.size === 0) rooms.delete(room);
    else broadcastPeers(room);
  }
}
function emit(id, room, topic, data) {
  const set = rooms.get(room);
  if (!set || !set.has(id)) return;
  const msg = { t: 'msg', room, topic, data, peer: id };
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
    if (clients.size >= MAX_CLIENTS) { res.writeHead(503); return res.end(); }
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache, no-transform',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no'
    });
    const id = crypto.randomBytes(6).toString('hex');
    const c = { res, rooms: new Map() };
    clients.set(id, c);
    write(c, { t: 'hello', id });
    const ka = setInterval(() => { try { res.write(': ka\n\n'); } catch (e) {} }, 20000);
    req.on('close', () => {
      clearInterval(ka);
      for (const room of [...c.rooms.keys()]) leave(id, room);
      clients.delete(id);
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
