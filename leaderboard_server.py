"""
لیدربورد مشترک «کی باهوش‌تره؟»  —  Flask + SQLite
------------------------------------------------
اجرا به‌صورت مستقل:   python leaderboard_server.py      (و باز کردن http://localhost:5000)
اتصال به سرور فعلی بازی: 
    from leaderboard_server import lb_bp
    app.register_blueprint(lb_bp)

آدرس‌ها:
    POST /lb/submit   {nm, s, n, m, pid}  -> ثبت نتیجه‌ی یک مسابقه
    GET  /lb/top?p=d|w|m|a&pid=...        -> برترین بازیکنان (امروز / این هفته / این ماه / همیشه)
بازه‌ها بر اساس وقت تهران و تقویم شمسی حساب می‌شوند:
    امروز = از ساعت ۰۰:۰۰ همین روز،  هفته = از شنبه،  ماه = از اول ماه شمسی.
"""
import os, re, sqlite3, time
from datetime import datetime, timedelta, timezone
from flask import Blueprint, Flask, jsonify, request, send_from_directory

DB = os.environ.get('LB_DB', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'lb.db'))
TEHRAN = timezone(timedelta(hours=3, minutes=30))   # ایران ساعت تابستانی ندارد
OK_N = (10, 25, 50)
OK_MODES = {'solo', 'ai', 'online', 'near'}
PID_RE = re.compile(r'^[a-z0-9]{6,32}$')
_last = {}            # ساده‌ترین محدودکننده‌ی نرخ ثبت

lb_bp = Blueprint('lb', __name__)


def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS scores(
        id INTEGER PRIMARY KEY AUTOINCREMENT, pid TEXT NOT NULL, nm TEXT NOT NULL,
        s INTEGER NOT NULL, n INTEGER NOT NULL, pc INTEGER NOT NULL, m TEXT NOT NULL, ts INTEGER NOT NULL)''')
    c.execute('CREATE INDEX IF NOT EXISTS i_ts ON scores(ts)')
    return c


def g2j(gy, gm, gd):
    """تبدیل تاریخ میلادی به شمسی."""
    gdm = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400 + gd + gdm[gm - 1]
    jy = -1595 + 33 * (days // 12053); days %= 12053
    jy += 4 * (days // 1461); days %= 1461
    if days > 365:
        jy += (days - 1) // 365; days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def period_start(p, now=None):
    """زمان شروع بازه (ثانیه‌ی یونیکس)؛ برای «همیشه» صفر."""
    if p == 'a':
        return 0
    now = now or datetime.now(TEHRAN)
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if p == 'd':
        start = day0
    elif p == 'w':      # شنبه = ۵ در weekday پایتون
        start = day0 - timedelta(days=(now.weekday() + 2) % 7)
    else:               # 'm'
        _, _, jd = g2j(now.year, now.month, now.day)
        start = day0 - timedelta(days=jd - 1)
    return int(start.timestamp())


def clean_name(s):
    s = re.sub(r'[<>&"\'\\\x00-\x1f]', '', str(s or '')).strip()[:14]
    return s or 'بازیکن'


@lb_bp.post('/lb/submit')
def submit():
    d = request.get_json(silent=True) or {}
    try:
        s, n = int(d.get('s')), int(d.get('n'))
    except (TypeError, ValueError):
        return jsonify(ok=False, e='bad'), 400
    pid, m = str(d.get('pid', '')), str(d.get('m', ''))
    if n not in OK_N or not 0 <= s <= n or m not in OK_MODES or not PID_RE.match(pid):
        return jsonify(ok=False, e='bad'), 400
    now = time.time()
    key = (pid, request.remote_addr)
    if now - _last.get(key, 0) < 8:          # حداقل ۸ ثانیه بین دو ثبت
        return jsonify(ok=False, e='rate'), 429
    _last[key] = now
    with db() as c:
        c.execute('INSERT INTO scores(pid,nm,s,n,pc,m,ts) VALUES(?,?,?,?,?,?,?)',
                  (pid, clean_name(d.get('nm')), s, n, round(s * 100 / n), m, int(now)))
    return jsonify(ok=True)


@lb_bp.get('/lb/top')
def top():
    p = request.args.get('p', 'd')
    if p not in ('d', 'w', 'm', 'a'):
        p = 'd'
    me = request.args.get('pid', '')
    with db() as c:
        rows = c.execute('SELECT pid,nm,s,n,pc,ts FROM scores WHERE ts>=? ORDER BY ts', (period_start(p),)).fetchall()
    by = {}
    for r in rows:
        u = by.setdefault(r['pid'], {'pid': r['pid'], 'nm': r['nm'], 'best': -1, 'bn': 0, 'bts': 0, 'games': 0, 'sum': 0})
        u['games'] += 1; u['sum'] += r['pc']; u['nm'] = r['nm']     # آخرین نام
        if (r['pc'], r['n']) > (u['best'], u['bn']):
            u['best'], u['bn'], u['bts'] = r['pc'], r['n'], r['ts']
    order = sorted(by.values(), key=lambda u: (-u['best'], -u['bn'], u['bts']))
    out, mine = [], None
    for i, u in enumerate(order, 1):
        row = {'rank': i, 'nm': u['nm'], 'best': u['best'], 'n': u['bn'], 'games': u['games'],
               'avg': round(u['sum'] / u['games']), 'me': u['pid'] == me}
        if row['me']:
            mine = row
        if i <= 20:
            out.append(row)
    return jsonify(p=p, players=out, me=mine, total=len(order))


def make_app():
    app = Flask(__name__)
    app.register_blueprint(lb_bp)
    here = os.path.dirname(os.path.abspath(__file__))

    @app.get('/')
    def index():
        return send_from_directory(here, 'quiz.html')
    return app


if __name__ == '__main__':
    make_app().run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
