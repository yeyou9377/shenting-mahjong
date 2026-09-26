#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""人机同桌 · 四川麻将（血战到底）毛坯服务 v0

单文件 Flask + SQLite，和「机不知道」论坛完全隔离：独立端口、独立 majiang.db。

运行：
    PORT=8791 python3 app.py

路由：
    GET  /mj/                         开桌页（人类用）
    GET  /mj/t/<table>/<token>        人类座位的手机网页
    POST /mj/api/new_table            建桌 {seats:[{name,type}]} -> 各座 token
    GET  /mj/api/state?table=&seat=&token=   该座位视角
    POST /mj/api/act                  {table,seat,token,action,tile}
    GET  /mj/api/health               活着吗
"""
import base64
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import hmac
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify, abort, Response, send_from_directory

import engine as E

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "majiang.db")
CST = timezone(timedelta(hours=8))
LOCK = threading.Lock()
MELD_NAME = {"peng": "碰", "gang_ming": "明杠", "gang_an": "暗杠", "gang_bu": "补杠"}
PLAYER_TIMEOUT_SEC = int(os.environ.get("MJ_PLAYER_TIMEOUT_SEC", "45"))
CREATE_WINDOW_SEC = 60
CREATE_LIMIT = int(os.environ.get("MJ_CREATE_LIMIT", "8"))
CREATE_ATTEMPTS = {}

# ---- 房间 / 候场（组局层；引擎不感知） ----
ROOM_WAIT_SEAT_SEC = int(os.environ.get("MJ_WAIT_SEAT_SEC", "90"))    # 候场占座者多久没心跳就释放
ROOM_RESERVE_SEC = 120          # 留座有效期
ROOM_WAIT_MAX_SEC = 1800        # 候场 30 分钟没开局解散
ROOM_OVER_MAX_SEC = 1800        # 终局后保留 30 分钟
ROOM_COUNTDOWN_SEC = 3          # 四座全准备 → 倒计时开局
ROOM_SETTINGS = {"mode": "血战到底", "players": 4, "rounds": 1,
                 "rule": ["底分1", "不封顶", "自摸×2"]}
GUEST_IDENT_RE = re.compile(r"^guest:[A-Za-z0-9_-]{6,40}$")

app = Flask(__name__)


# ---------- 存储 ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tables (
                id          TEXT PRIMARY KEY,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                tokens      TEXT NOT NULL,   -- {token: seat}
                state       TEXT NOT NULL    -- engine 的 game dict
            );
            """
        )
        # 组局层：旧库补 phase / room 两列。旧桌 phase=playing、room={}，不进大厅，不被清理。
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tables)").fetchall()}
        if "phase" not in cols:
            conn.execute("ALTER TABLE tables ADD COLUMN phase TEXT NOT NULL DEFAULT 'playing'")
        if "room" not in cols:
            conn.execute("ALTER TABLE tables ADD COLUMN room TEXT NOT NULL DEFAULT '{}'")


def create_key_configured():
    return bool(os.environ.get("MJ_CREATE_KEY"))


def check_create_permission(req):
    """Production can require MJ_CREATE_KEY; local dev remains frictionless."""
    key = os.environ.get("MJ_CREATE_KEY")
    if key:
        supplied = req.headers.get("X-MJ-Create-Key") or (req.get_json(silent=True) or {}).get("create_key", "")
        if not supplied or not hmac.compare_digest(str(supplied), key):
            return False, "建桌需要有效的创建密钥"
    return check_rate(req, CREATE_LIMIT, "建桌太频繁，请稍后再试")


def check_rate(req, limit, msg="太频繁了，歇一下再来"):
    """每 IP 每分钟 limit 次。组局接口只限流不要密钥：家长没有账号，浏览器直连。"""
    ip = req.headers.get("CF-Connecting-IP") or \
        req.headers.get("X-Forwarded-For", req.remote_addr or "-").split(",")[0].strip()
    now_ts = time.time()
    attempts = [t for t in CREATE_ATTEMPTS.get(ip, []) if now_ts - t < CREATE_WINDOW_SEC]
    if len(attempts) >= limit:
        CREATE_ATTEMPTS[ip] = attempts
        return False, msg
    attempts.append(now_ts)
    CREATE_ATTEMPTS[ip] = attempts
    return True, None


def touch_player(g, seat, *, reconnected=False):
    p = g["players"][seat]
    p["last_seen"] = time.time()
    if p.get("autoplay"):
        p["autoplay"] = False
        if reconnected:
            g["log"].append(f"{p['name']} 重新连接，解除托管")


def apply_timeouts(g):
    """将长时间没有请求的真人/API座位交给笨 bot，避免全桌陪等。"""
    if g.get("phase") == "over":
        return False
    changed = False
    now_ts = time.time()
    for p in g["players"]:
        if p.get("type") == "bot" or p.get("autoplay"):
            continue
        seen = p.get("last_seen")
        if seen is not None and now_ts - seen >= PLAYER_TIMEOUT_SEC:
            p["autoplay"] = True
            p["disconnected_at"] = now_ts
            g["log"].append(f"{p['name']} 超时，已由笨机托管")
            changed = True
    return changed


def controlled_by_bot(p):
    return p.get("type") == "bot" or p.get("autoplay", False)


# ---------- 本手摸牌（drawn_tile）----------
# engine.py 不动：在 app 层包一下 E._draw_to（引擎内部按模块全局名调用，摸牌/杠后补张都经过它），
# 把"谁、摸了哪张、摸完时的局面签名"记进 g["drawn"]。视角里只在签名仍吻合时给出，
# 碰牌后轮到自己（没摸牌）、别人的回合、定缺阶段都是 null。庄家起手 14 张没有"摸"这一步，也是 null。
_engine_draw_to = E._draw_to


def _draw_sig(g):
    return [len(g["deck"]), sum(len(p["melds"]) for p in g["players"]), g["hu_seq"]]


def _draw_to_tracked(g, seat):
    tile = g["deck"][0] if g["deck"] else None
    _engine_draw_to(g, seat)
    if tile is not None:
        g["drawn"] = {"seat": seat, "tile": tile, "sig": _draw_sig(g)}


E._draw_to = _draw_to_tracked


def drawn_for(g, seat):
    d = g.get("drawn")
    if not d or g["phase"] != "play" or g["step"] != "discard" or g["turn"] != seat or d["seat"] != seat:
        return None
    if d["sig"] != _draw_sig(g) or d["tile"] not in g["players"][seat]["hand"]:
        return None
    return d["tile"]


def add_drawn(v, g, seat):
    t = drawn_for(g, seat)
    v["drawn_tile"] = t
    v["drawn_tile_text"] = E.tile_str(t) if t else None
    return v


def now():
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


def row_to_table(r):
    state = json.loads(r["state"]) if r["state"] else None
    if not state:
        state = None
    return {"id": r["id"], "tokens": json.loads(r["tokens"]), "state": state,
            "phase": r["phase"], "room": json.loads(r["room"] or "{}")}


def load_table(tid):
    with db() as conn:
        r = conn.execute("SELECT * FROM tables WHERE id=?", (tid,)).fetchone()
    if not r:
        return None
    return row_to_table(r)


def save_table(tid, state):
    with db() as conn:
        conn.execute("UPDATE tables SET state=?, updated_at=? WHERE id=?",
                     (json.dumps(state, ensure_ascii=False), now(), tid))


def save_room(t):
    """整桌写回：phase / room / tokens / state。组局层所有变更走这里。"""
    with db() as conn:
        conn.execute(
            "UPDATE tables SET phase=?, room=?, tokens=?, state=?, updated_at=? WHERE id=?",
            (t["phase"], json.dumps(t["room"], ensure_ascii=False), json.dumps(t["tokens"]),
             json.dumps(t["state"] or {}, ensure_ascii=False), now(), t["id"]))


def delete_table(tid):
    with db() as conn:
        conn.execute("DELETE FROM tables WHERE id=?", (tid,))


def auth(tid, seat, token):
    t = load_table(tid)
    if not t:
        return None, None, ("桌子不存在", 404)
    if token not in t["tokens"]:
        return None, None, ("token 不对", 403)
    s = t["tokens"][token]
    if seat is not None and int(seat) != s:
        return None, None, ("这个 token 不是这个座位的", 403)
    return t, s, None


# ---------- 组局层：身份 / 房间 / 座位 ----------
# 座位中性：room["seats"][0..3] = 东南西北，谁都能坐任何空座。
# 座位条目：None（空）| {"kind":"reserved","for_ident","for_name","until"}
#          | {"kind":"human"|"machine"|"bot","ident","name","ready","last_seen","machine_of","partner_of"}
# 人机配对（partner_of）只是关系标记，由 machine_of 推出，不参与座位分配。

def verify_pass(p):
    """论坛签发的身份凭证：base64url(json).hex(hmac_sha256(MJ_CREATE_KEY, json))。
    密钥只在两个服务端之间共享，前端永远看不到。"""
    key = os.environ.get("MJ_CREATE_KEY", "")
    if not key or not isinstance(p, str) or "." not in p:
        return None
    b, sig = p.rsplit(".", 1)
    try:
        raw = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4))
    except Exception:  # noqa: BLE001
        return None
    if not hmac.compare_digest(hmac.new(key.encode(), raw, hashlib.sha256).hexdigest(), sig):
        return None
    try:
        info = json.loads(raw)
    except Exception:  # noqa: BLE001
        return None
    if not info.get("ident") or not info.get("name") or info.get("exp", 0) < time.time():
        return None
    return info


def resolve_identity(body):
    """→ (who, err)。有 pass 走论坛身份；没有就是 guest（家长没账号，填个名字就能坐）。"""
    p = body.get("pass")
    if p:
        info = verify_pass(p)
        if not info:
            return None, "身份凭证无效或已过期"
        kind = "machine" if info.get("role") == "machine" else "human"
        return {"kind": kind, "ident": str(info["ident"])[:60], "name": str(info["name"]).strip()[:12] or "无名",
                "machine_of": str(info.get("machine_of") or "")[:40]}, None
    ident = (body.get("ident") or "").strip()
    if not GUEST_IDENT_RE.match(ident):
        ident = "guest:" + secrets.token_urlsafe(9)
    kind = "machine" if (body.get("kind") or "") == "machine" else "human"
    nick = (body.get("nick") or "").strip()[:12]
    if not nick:
        nick = ("小机" if kind == "machine" else "路人") + ident[-4:]
    return {"kind": kind, "ident": ident, "name": nick, "machine_of": ""}, None


def new_room_code():
    with db() as conn:
        for _ in range(50):
            code = str(secrets.randbelow(900000) + 100000)
            if not conn.execute("SELECT 1 FROM tables WHERE id=?", (code,)).fetchone():
                return code
    return secrets.token_hex(3)


def occupied_seats(r):
    return [s for s in r["seats"] if s and s["kind"] != "reserved"]


def seat_token(t, seat):
    for tk, s in t["tokens"].items():
        if s == seat:
            return tk
    return None


def relink_partners(r):
    """小机的 machine_of 指向家长 username → 家长在同一房时打上关系标记。"""
    by_ident = {s["ident"]: s for s in occupied_seats(r)}
    for s in occupied_seats(r):
        s["partner_of"] = None
        s["partner_of_name"] = None
        if s["kind"] == "machine" and s.get("machine_of"):
            h = by_ident.get("human:" + s["machine_of"])
            if h:
                s["partner_of"] = h["ident"]
                s["partner_of_name"] = h["name"]


def eff_phase(t):
    if t["phase"] == "waiting":
        return "waiting"
    if t["state"] and t["state"].get("phase") == "over":
        return "over"
    return t["phase"]


def room_public(t, my_seat=None):
    """房间公开信息：给大厅和候场页。永远不含 state / 手牌。"""
    r = t["room"]
    seats = []
    for i, s in enumerate(r["seats"]):
        d = {"seat": i, "dir": E.SEAT_NAMES[i]}
        if s is None:
            d["state"] = "empty"
        elif s["kind"] == "reserved":
            d.update({"state": "reserved", "for_name": s["for_name"],
                      "left": max(0, int(s["until"] - time.time()))})
        else:
            d.update({"state": "ready" if s.get("ready") else "seated", "kind": s["kind"],
                      "name": s["name"], "partner_of": s.get("partner_of_name"),
                      "is_host": i == r["host_seat"]})
        seats.append(d)
    occ = occupied_seats(r)
    out = {"room": t["id"], "name": r["name"], "visible": r["visible"], "phase": eff_phase(t),
           "host_seat": r["host_seat"], "host_name": (r["seats"][r["host_seat"]] or {}).get("name"),
           "settings": r["settings"], "seats": seats,
           "occupied": len(occ), "need": 4 - len(occ),
           "created_at": r["created_at"], "log": r.get("log", [])[-8:],
           "watching": len(r.get("watchers") or {})}
    if r.get("countdown_at"):
        out["countdown_left"] = max(0, ROOM_COUNTDOWN_SEC - int(time.time() - r["countdown_at"]))
    if t["state"]:
        out["wall"] = len(t["state"]["deck"])
        out["turn_no"] = sum(len(p["discards"]) for p in t["state"]["players"])
    if my_seat is not None:
        out["my_seat"] = my_seat
        out["is_host"] = my_seat == r["host_seat"]
        out["can_start"] = bool(occ) and all(s.get("ready") for s in occ)
    return out


def room_log(r, msg):
    r.setdefault("log", []).append(msg)
    r["log"] = r["log"][-30:]


def find_ident_rooms(ident):
    """这个身份现在坐在哪些候场房里（用于"同一身份只在一房"）。"""
    hits = []
    with db() as conn:
        rows = conn.execute("SELECT * FROM tables WHERE phase='waiting' AND room!='{}'").fetchall()
    for rw in rows:
        t = row_to_table(rw)
        for i, s in enumerate(t["room"]["seats"]):
            if s and s["kind"] != "reserved" and s["ident"] == ident:
                hits.append((t, i))
    return hits


def remove_seat(t, seat, reason=None):
    r = t["room"]
    s = r["seats"][seat]
    if not s:
        return
    tk = seat_token(t, seat)
    if tk:
        del t["tokens"][tk]
    r["seats"][seat] = None
    if s["kind"] != "reserved" and reason:
        room_log(r, f"{s['name']} {reason}")
    # 房主走了 → 交给最早入座的人
    if r["host_seat"] == seat:
        occ = [(x.get("joined_at", 0), i) for i, x in enumerate(r["seats"]) if x and x["kind"] != "reserved"]
        if occ:
            r["host_seat"] = min(occ)[1]
            room_log(r, f"{r['seats'][r['host_seat']]['name']} 成为房主")
    relink_partners(r)


def create_room(who, name="", visible=True):
    r = {"code": None, "name": (name or "").strip()[:12] or f"{who['name']}的局", "visible": bool(visible),
         "host_seat": 0, "settings": dict(ROOM_SETTINGS), "seats": [None, None, None, None],
         "created_at": time.time(), "started_at": None, "countdown_at": None, "log": []}
    code = new_room_code()
    r["code"] = code
    t = {"id": code, "tokens": {}, "state": None, "phase": "waiting", "room": r}
    with db() as conn:
        conn.execute("INSERT INTO tables (id, created_at, updated_at, tokens, state, phase, room) VALUES (?,?,?,?,?,?,?)",
                     (code, now(), now(), "{}", "{}", "waiting", json.dumps(r, ensure_ascii=False)))
    seat, tk = join_room(t, who, seat=0)
    return t, seat, tk


def join_room(t, who, seat=None):
    """→ (seat, token) 或抛 ValueError。同一身份重复 join 直接拿回原座原 token。"""
    r = t["room"]
    for i, s in enumerate(r["seats"]):
        if s and s["kind"] != "reserved" and s["ident"] == who["ident"]:
            s["last_seen"] = time.time()
            return i, seat_token(t, i)
    if t["phase"] != "waiting":
        raise ValueError("这桌已经开打了")
    # 换房：从别的候场房里起身
    for other, i in find_ident_rooms(who["ident"]):
        if other["id"] != t["id"]:
            remove_seat(other, i, "去了别的房")
            save_room(other)
    if seat is None:
        # 留给我的座位优先，其次第一个空座
        mine = [i for i, s in enumerate(r["seats"]) if s and s["kind"] == "reserved" and reserved_for(s, who)]
        empty = [i for i, s in enumerate(r["seats"]) if s is None]
        cands = mine + empty
        if not cands:
            raise ValueError("这桌满了")
        seat = cands[0]
    seat = int(seat)
    if not 0 <= seat <= 3:
        raise ValueError("座位号不对")
    cur = r["seats"][seat]
    if cur is not None and not (cur["kind"] == "reserved" and reserved_for(cur, who)):
        raise ValueError("这个座位有人了")
    tk = secrets.token_urlsafe(32)
    r["seats"][seat] = {"kind": who["kind"], "ident": who["ident"], "name": who["name"],
                        "machine_of": who.get("machine_of", ""), "partner_of": None, "partner_of_name": None,
                        "ready": who["kind"] == "machine", "last_seen": time.time(), "joined_at": time.time()}
    t["tokens"][tk] = seat
    room_log(r, f"{who['name']} 坐到{E.SEAT_NAMES[seat]}座")
    relink_partners(r)
    return seat, tk


def reserved_for(s, who):
    f = s["for_ident"]
    return f == who["ident"] or f == who["kind"] + ":*"


def start_room(t):
    r = t["room"]
    seats = []
    ts = time.time()
    for i, s in enumerate(r["seats"]):
        if s is None or s["kind"] == "reserved":
            nm = f"笨机{E.SEAT_NAMES[i]}"
            r["seats"][i] = {"kind": "bot", "ident": f"bot:{i}", "name": nm, "ready": True,
                             "last_seen": ts, "joined_at": ts, "machine_of": "", "partner_of": None, "partner_of_name": None}
            seats.append({"name": nm, "type": "bot"})
        else:
            seats.append({"name": s["name"], "type": "api" if s["kind"] == "machine" else "human"})
    g = E.new_game(seats)
    for i, p in enumerate(g["players"]):
        p["last_seen"] = ts
        p["autoplay"] = False
        p["disconnected_at"] = None
        p["ident"] = r["seats"][i]["ident"]
        p["partner_of"] = r["seats"][i].get("partner_of_name")
    E.run_bots(g, limit=5000)
    t["state"] = g
    t["phase"] = "playing"
    r["started_at"] = ts
    r["countdown_at"] = None
    room_log(r, "开局")


def room_housekeep(t):
    """候场超时释放 / 留座过期 / 全员准备倒计时开局 / 旧房清理。返回 'delete' 表示该删。"""
    r = t["room"]
    if not r:
        return None
    ts = time.time()
    if t["phase"] == "waiting":
        if ts - r["created_at"] > ROOM_WAIT_MAX_SEC:
            return "delete"
        for i, s in enumerate(r["seats"]):
            if s is None:
                continue
            if s["kind"] == "reserved":
                if ts > s["until"]:
                    r["seats"][i] = None
            elif ts - s["last_seen"] > ROOM_WAIT_SEAT_SEC:
                remove_seat(t, i, "掉线离开了")
        occ = occupied_seats(r)
        if not occ:
            return "delete"
        if len(occ) == 4 and all(s.get("ready") for s in occ):
            if not r.get("countdown_at"):
                r["countdown_at"] = ts
                room_log(r, "四座齐了，3 秒后开局")
            elif ts - r["countdown_at"] >= ROOM_COUNTDOWN_SEC:
                start_room(t)
        else:
            r["countdown_at"] = None
    else:
        if t["state"] and t["state"].get("phase") == "over":
            if t["phase"] != "over":
                t["phase"] = "over"
                r["over_at"] = ts
            elif ts - r.get("over_at", ts) > ROOM_OVER_MAX_SEC:
                return "delete"
    return None


def room_auth(body):
    """组局接口鉴权：room/table + token。→ (t, seat, err)"""
    tid = str(body.get("room") or body.get("table") or "")
    token = body.get("token") or ""
    t, s, err = auth(tid, None, token)
    if err:
        return None, None, err
    if not t["room"]:
        return None, None, ("这是旧式快速桌，没有候场", 400)
    return t, s, None


def room_reply(t, seat, extra=None):
    out = {"ok": True, "room": t["id"], "table": t["id"], "phase": eff_phase(t),
           "seat": seat, "seat_name": E.SEAT_NAMES[seat], "token": seat_token(t, seat),
           "web_url": f"/mj/t/{t['id']}/{seat_token(t, seat)}",
           "state_url": f"/mj/api/state?table={t['id']}&seat={seat}&token={seat_token(t, seat)}"}
    out.update(extra or {})
    return jsonify(out)


# ---------- 观战席：只读、不占座、不计入 occupied、不影响开局 ----------
# room["watchers"] = {watch_token: {"created": ts, "last_seen": ts}}。watch token 不在 t["tokens"] 里，
# 所以 act / ready / leave / 座位网页 全部天然 403/404。
WATCH_MAX = 64


def add_watcher(t):
    ws = t["room"].setdefault("watchers", {})
    tk = "w" + secrets.token_urlsafe(24)
    ts = time.time()
    ws[tk] = {"created": ts, "last_seen": ts}
    if len(ws) > WATCH_MAX:          # 只留最近活跃的 WATCH_MAX 个
        keep = sorted(ws.items(), key=lambda kv: kv[1]["last_seen"])[-WATCH_MAX:]
        t["room"]["watchers"] = dict(keep)
    return tk


def watch_auth(tid, wt):
    """→ (t, err)。只认新式房里登记过的观战 token。"""
    t = load_table(tid)
    if not t or not t["room"]:
        return None, ("没有这个房", 404)
    if not wt or wt not in (t["room"].get("watchers") or {}):
        return None, ("观战链接不对或已失效", 403)
    return t, None


def spectator_view(g, pov=0):
    """观战视角：以 pov 座为底边排版，但任何人的手牌都不给。
    在 E.view 的基础上把所有"本座私有"字段清空，并隐藏定缺阶段还没公开的缺门。"""
    v = E.view(g, pov)
    v.update({"spectator": True, "my_name": "观战", "my_que": None, "my_hu": None,
              "hand": [], "hand_text": [], "melds": [], "available_actions": [],
              "drawn_tile": None, "drawn_tile_text": None})
    for p in v["players"]:
        p["is_me"] = False
        p["partner_of"] = g["players"][p["seat"]].get("partner_of")
        if g["phase"] == "que":
            p["que"] = None
    return v


def watch_urls(t, tk):
    return {"watch": tk, "watch_url": f"/mj/w/{t['id']}/{tk}",
            "state_url": f"/mj/api/state?table={t['id']}&watch={tk}"}


# ---------- API ----------

@app.route("/mj/api/health")
def health():
    return jsonify({"ok": True, "service": "majiang", "time": now()})


@app.route("/mj/static/sfx/<path:fn>")
def sfx_file(fn):
    """音效占位文件。走 /mj/ 前缀，代理后依然可达；只服务 static/sfx 目录。"""
    return send_from_directory(os.path.join(APP_DIR, "static", "sfx"), fn)


@app.route("/mj/api/new_table", methods=["POST"])
def api_new_table():
    allowed, reason = check_create_permission(request)
    if not allowed:
        return jsonify({"ok": False, "error": reason}), 403 if "密钥" in reason else 429
    body = request.get_json(silent=True) or {}
    seats = body.get("seats") or []
    if len(seats) > 4:
        return jsonify({"ok": False, "error": "最多 4 个座位"}), 400
    while len(seats) < 4:
        seats.append({"name": f"笨机{len(seats)}", "type": "bot"})
    clean = []
    for i, s in enumerate(seats):
        typ = (s.get("type") or "bot").lower()
        if typ not in ("human", "bot", "api"):
            return jsonify({"ok": False, "error": f"座位 {i} 的 type 只能是 human/bot/api"}), 400
        clean.append({"name": (s.get("name") or "").strip()[:12] or f"{E.SEAT_NAMES[i]}家",
                      "type": typ})

    tid = secrets.token_hex(3)
    seed = body.get("seed")
    g = E.new_game(clean, seed=seed)
    for p in g["players"]:
        p["last_seen"] = time.time()
        p["autoplay"] = False
        p["disconnected_at"] = None
    tokens = {}
    out = []
    for i, s in enumerate(clean):
        tk = secrets.token_urlsafe(32)
        tokens[tk] = i
        out.append({"seat": i, "seat_name": E.SEAT_NAMES[i], "name": s["name"],
                    "type": s["type"], "token": tk,
                    "web_url": f"/mj/t/{tid}/{tk}"})
    with LOCK:
        with db() as conn:
            conn.execute(
                "INSERT INTO tables (id, created_at, updated_at, tokens, state) VALUES (?,?,?,?,?)",
                (tid, now(), now(), json.dumps(tokens), json.dumps(g, ensure_ascii=False)))
        E.run_bots(g, limit=5000)
        save_table(tid, g)
    return jsonify({"ok": True, "table": tid, "dealer": 0, "seats": out})


@app.route("/mj/api/state")
def api_state():
    tid = request.args.get("table", "") or request.args.get("room", "")
    token = request.args.get("token", "")
    seat = request.args.get("seat")
    wt = request.args.get("watch", "")
    if wt and not token:
        return watch_state(tid, wt, seat)
    with LOCK:
        t, s, err = auth(tid, seat, token)
        if err:
            # 房间解散 / 座位被释放后，旧 token 都会走到这里；gone 让网页回大厅
            return jsonify({"ok": False, "error": err[0], "gone": True}), err[1]
        if t["room"]:
            if room_housekeep(t) == "delete":
                delete_table(tid)
                return jsonify({"ok": False, "error": "房间已解散", "gone": True}), 410
            if t["phase"] == "waiting":
                if token not in t["tokens"]:      # 自己刚被超时释放
                    save_room(t)
                    return jsonify({"ok": False, "error": "你的座位因长时间没响应已释放", "gone": True}), 410
                t["room"]["seats"][s]["last_seen"] = time.time()
                save_room(t)
                return jsonify({"ok": True, "table": tid, "phase": "waiting", "my_seat": s,
                                "room": room_public(t, s)})
        g = t["state"]
        apply_timeouts(g)
        touch_player(g, s, reconnected=True)
        E.run_bots(g, limit=5000)
        if t["room"]:
            room_housekeep(t)      # 刚打完 → phase=over 记时间
            save_room(t)
        else:
            save_table(tid, g)
        v = E.view(g, s)
        for p in v["players"]:
            p["partner_of"] = g["players"][p["seat"]].get("partner_of")
        v["fp"] = state_fp(g)
        add_drawn(v, g, s)
    v.update({"ok": True, "table": tid})
    if t["room"]:
        v["room"] = room_public(t, s)
    return jsonify(v)


def watch_state(tid, wt, seat=None):
    """观战者轮询：推进超时/笨机（和玩家轮询一样不会卡桌），但不 touch 任何座位。"""
    try:
        pov = int(seat) if seat not in (None, "") else 0
    except (TypeError, ValueError):
        pov = 0
    pov = pov if 0 <= pov <= 3 else 0
    with LOCK:
        t, err = watch_auth(tid, wt)
        if err:
            return jsonify({"ok": False, "error": err[0], "gone": True}), err[1]
        if room_housekeep(t) == "delete":
            delete_table(tid)
            return jsonify({"ok": False, "error": "房间已解散", "gone": True}), 410
        w = t["room"]["watchers"].get(wt)
        if w:
            w["last_seen"] = time.time()
        if t["phase"] == "waiting" or not t["state"]:
            save_room(t)
            return jsonify({"ok": True, "table": tid, "phase": "waiting", "spectator": True,
                            "my_seat": pov, "hand": [], "hand_text": [], "room": room_public(t)})
        g = t["state"]
        apply_timeouts(g)
        E.run_bots(g, limit=5000)
        room_housekeep(t)
        save_room(t)
        v = spectator_view(g, pov)
        v["fp"] = state_fp(g)
    v.update({"ok": True, "table": tid, "room": room_public(t)})
    return jsonify(v)


@app.route("/mj/api/watch", methods=["POST"])
def api_watch():
    """领一张观战票：不占座、不计入 occupied、不影响开局。"""
    allowed, reason = check_rate(request, 20)
    if not allowed:
        return jsonify({"ok": False, "error": reason}), 429
    body = request.get_json(silent=True) or {}
    code = str(body.get("room") or body.get("table") or "").strip()
    with LOCK:
        t = load_table(code)
        if not t or not t["room"]:
            return jsonify({"ok": False, "error": "没有这个房号"}), 404
        if room_housekeep(t) == "delete":
            delete_table(code)
            return jsonify({"ok": False, "error": "房间已解散"}), 410
        tk = add_watcher(t)
        save_room(t)
        out = {"ok": True, "room": t["id"], "table": t["id"], "phase": eff_phase(t)}
        out.update(watch_urls(t, tk))
        return jsonify(out)


@app.route("/mj/api/act", methods=["POST"])
def api_act():
    body = request.get_json(silent=True) or {}
    tid = body.get("table", "")
    token = body.get("token", "")
    seat = body.get("seat")
    action = (body.get("action") or "").strip()
    tile = body.get("tile")
    rid = str(body.get("request_id") or "")[:64]
    client_fp = str(body.get("fp") or "")[:80]
    with LOCK:
        t, s, err = auth(tid, seat, token)
        if err:
            return jsonify({"ok": False, "error": err[0]}), err[1]
        g = t["state"]
        if not g:
            return jsonify({"ok": False, "error": "还没开局，先在候场准备"}), 409
        apply_timeouts(g)
        touch_player(g, s, reconnected=True)
        # ---- 幂等：响应丢了客户端重发，不能重复出手 ----
        # 1) request_id 命中过 → 直接回当前视角
        # 2) 没带 request_id 但带了 fp（客户端上次看到的状态指纹）：同座同动作同牌、且 fp 等于
        #    上次成功出手前的指纹 → 说明是从旧状态重发的同一手
        seen = g.setdefault("acts_seen", [])
        last = g.setdefault("last_act", {}).get(str(s))
        dup = (rid and rid in seen) or (
            not rid and client_fp and last and last.get("action") == action and last.get("tile") == tile
            and last.get("fp_before") == client_fp)
        if not dup:
            fp_before = state_fp(g)
            try:
                E.act(g, s, action, tile)
                E.run_bots(g, limit=5000)
            except E.MJError as e:
                return jsonify({"ok": False, "error": str(e), "fp": fp_before}), 400
            except Exception as e:  # noqa: BLE001
                return jsonify({"ok": False, "error": f"引擎出错：{e}"}), 500
            g["last_act"][str(s)] = {"action": action, "tile": tile, "fp_before": fp_before, "rid": rid}
            if rid:
                seen.append(rid)
                del seen[:-ACT_MEMO_MAX]
            if t["room"]:
                room_housekeep(t)
                save_room(t)
            else:
                save_table(tid, g)
        v = act_view(g, s)
    v.update({"ok": True, "table": tid, "dup": bool(dup)})
    return jsonify(v)


ACT_MEMO_MAX = 64


def state_fp(g):
    """牌局指纹：任何一次成功出手（含随后的 bot 连锁）都会改变它。"""
    claim = g.get("claim") or {}
    parts = (g["phase"], g["step"], g["turn"], len(g["deck"]), g["hu_seq"],
             sum(len(p["discards"]) for p in g["players"]),
             sum(len(p["melds"]) for p in g["players"]),
             sum(1 for p in g["players"] if p["que"]),
             len(claim.get("responses", {}) or {}))
    return "-".join(str(x) for x in parts)


def act_view(g, s):
    """act 的响应：本座视角，但去掉 25 行流水（只留 log_tail 3 行），瘦一半以上；完整流水走 state。"""
    v = E.view(g, s)
    for p in v["players"]:
        p["partner_of"] = g["players"][p["seat"]].get("partner_of")
    v["log_tail"] = v["log"][-3:]
    del v["log"]
    for p in v["players"]:
        p.pop("discards_text", None)      # 网页用 discards 自己画；文字版看 state
    for h in v["hand_text"]:
        h.pop("s", None)                  # s = e+n 的拼接，冗余
    v["fp"] = state_fp(g)
    v["slim"] = True
    add_drawn(v, g, s)
    return v


# ---------- 组局 API（大厅 / 候场） ----------

def list_rooms(include_private=False):
    """所有新式房（顺手做清理）。"""
    with db() as conn:
        rows = conn.execute("SELECT * FROM tables WHERE room!='{}' ORDER BY created_at").fetchall()
    out = []
    for rw in rows:
        t = row_to_table(rw)
        if room_housekeep(t) == "delete":
            delete_table(t["id"])
            continue
        save_room(t)
        if t["room"].get("visible") or include_private:
            out.append(t)
    return out


@app.route("/mj/api/rooms")
def api_rooms():
    with LOCK:
        ts = list_rooms()
        rooms = [room_public(t) for t in ts if eff_phase(t) != "over"]
    return jsonify({"ok": True, "rooms": rooms,
                    "waiting": sum(1 for r in rooms if r["phase"] == "waiting"),
                    "playing": sum(1 for r in rooms if r["phase"] == "playing")})


@app.route("/mj/api/room")
def api_room():
    code = request.args.get("room") or request.args.get("table") or ""
    with LOCK:
        t = load_table(code)
        if not t or not t["room"]:
            return jsonify({"ok": False, "error": "没有这个房"}), 404
        if room_housekeep(t) == "delete":
            delete_table(code)
            return jsonify({"ok": False, "error": "房间已解散"}), 410
        save_room(t)
        return jsonify({"ok": True, "room": room_public(t)})


@app.route("/mj/api/rooms", methods=["POST"])
def api_create_room():
    allowed, reason = check_rate(request, CREATE_LIMIT, "建房太频繁，歇一分钟再来")
    if not allowed:
        return jsonify({"ok": False, "error": reason}), 429
    body = request.get_json(silent=True) or {}
    who, err = resolve_identity(body)
    if err:
        return jsonify({"ok": False, "error": err}), 403
    with LOCK:
        for other, i in find_ident_rooms(who["ident"]):
            remove_seat(other, i, "去开了新房")
            save_room(other)
        t, seat, tk = create_room(who, body.get("name") or "", body.get("visible", True))
        save_room(t)
        return room_reply(t, seat, {"ident": who["ident"], "created": True, "room_info": room_public(t, seat)})


@app.route("/mj/api/queue", methods=["POST"])
def api_queue():
    """快速加入：最早的、有空座的公开候场房；没有就替你开一房。"""
    allowed, reason = check_rate(request, 20)
    if not allowed:
        return jsonify({"ok": False, "error": reason}), 429
    body = request.get_json(silent=True) or {}
    who, err = resolve_identity(body)
    if err:
        return jsonify({"ok": False, "error": err}), 403
    with LOCK:
        # 已经在某个候场房 → 就回那房
        for t, i in find_ident_rooms(who["ident"]):
            return room_reply(t, i, {"ident": who["ident"], "created": False})
        for t in list_rooms():
            if t["phase"] != "waiting":
                continue
            try:
                seat, tk = join_room(t, who)
            except ValueError:
                continue
            save_room(t)
            return room_reply(t, seat, {"ident": who["ident"], "created": False})
        t, seat, tk = create_room(who, "随便打打", True)
        save_room(t)
        return room_reply(t, seat, {"ident": who["ident"], "created": True})


@app.route("/mj/api/join", methods=["POST"])
def api_join():
    allowed, reason = check_rate(request, 20)
    if not allowed:
        return jsonify({"ok": False, "error": reason}), 429
    body = request.get_json(silent=True) or {}
    code = str(body.get("room") or body.get("table") or "").strip()
    who, err = resolve_identity(body)
    if err:
        return jsonify({"ok": False, "error": err}), 403
    with LOCK:
        t = load_table(code)
        if not t or not t["room"]:
            return jsonify({"ok": False, "error": "没有这个房号"}), 404
        if room_housekeep(t) == "delete":
            delete_table(code)
            return jsonify({"ok": False, "error": "房间已解散"}), 410
        try:
            seat, tk = join_room(t, who, body.get("seat"))
        except ValueError as e:
            save_room(t)
            return jsonify({"ok": False, "error": str(e)}), 409
        save_room(t)
        return room_reply(t, seat, {"ident": who["ident"]})


@app.route("/mj/api/sit", methods=["POST"])
def api_sit():
    body = request.get_json(silent=True) or {}
    with LOCK:
        t, s, err = room_auth(body)
        if err:
            return jsonify({"ok": False, "error": err[0]}), err[1]
        if t["phase"] != "waiting":
            return jsonify({"ok": False, "error": "开打了不能换座"}), 409
        try:
            to = int(body.get("seat"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "座位号不对"}), 400
        r = t["room"]
        if not 0 <= to <= 3 or to == s:
            return jsonify({"ok": False, "error": "座位号不对"}), 400
        me = r["seats"][s]
        cur = r["seats"][to]
        if cur is not None and not (cur["kind"] == "reserved" and reserved_for(cur, me)):
            return jsonify({"ok": False, "error": "那个座位有人了"}), 409
        r["seats"][to] = me
        r["seats"][s] = None
        t["tokens"][body["token"]] = to
        if r["host_seat"] == s:
            r["host_seat"] = to
        me["ready"] = False if me["kind"] != "machine" else True
        room_log(r, f"{me['name']} 换到{E.SEAT_NAMES[to]}座")
        relink_partners(r)
        save_room(t)
        return jsonify({"ok": True, "seat": to, "room": room_public(t, to)})


@app.route("/mj/api/ready", methods=["POST"])
def api_ready():
    body = request.get_json(silent=True) or {}
    with LOCK:
        t, s, err = room_auth(body)
        if err:
            return jsonify({"ok": False, "error": err[0]}), err[1]
        if t["phase"] != "waiting":
            return jsonify({"ok": False, "error": "已经开局了"}), 409
        me = t["room"]["seats"][s]
        me["ready"] = bool(body.get("ready", True))
        me["last_seen"] = time.time()
        room_log(t["room"], f"{me['name']} {'准备好了' if me['ready'] else '取消准备'}")
        room_housekeep(t)
        save_room(t)
        return jsonify({"ok": True, "phase": eff_phase(t), "room": room_public(t, s)})


@app.route("/mj/api/reserve", methods=["POST"])
def api_reserve():
    """留座：for_ident 可以是具体身份，或 machine:* / human:*。for_ident 为空 = 取消留座。"""
    body = request.get_json(silent=True) or {}
    with LOCK:
        t, s, err = room_auth(body)
        if err:
            return jsonify({"ok": False, "error": err[0]}), err[1]
        if t["phase"] != "waiting":
            return jsonify({"ok": False, "error": "已经开局了"}), 409
        r = t["room"]
        try:
            to = int(body.get("seat"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "座位号不对"}), 400
        cur = r["seats"][to] if 0 <= to <= 3 else "x"
        if cur is not None and (cur == "x" or cur["kind"] != "reserved"):
            return jsonify({"ok": False, "error": "只能给空座留座"}), 409
        f = (body.get("for_ident") or "").strip()[:60]
        if not f:
            r["seats"][to] = None
        else:
            r["seats"][to] = {"kind": "reserved", "for_ident": f, "until": time.time() + ROOM_RESERVE_SEC,
                              "for_name": (body.get("for_name") or "").strip()[:12] or
                              ("任何小机" if f == "machine:*" else "任何人" if f == "human:*" else f.split(":", 1)[-1])}
            room_log(r, f"{r['seats'][s]['name']} 把{E.SEAT_NAMES[to]}座留给 {r['seats'][to]['for_name']}")
        save_room(t)
        return jsonify({"ok": True, "room": room_public(t, s)})


@app.route("/mj/api/leave", methods=["POST"])
def api_leave():
    body = request.get_json(silent=True) or {}
    with LOCK:
        t, s, err = room_auth(body)
        if err:
            return jsonify({"ok": False, "error": err[0]}), err[1]
        if t["phase"] != "waiting":
            return jsonify({"ok": False, "error": "开打了不能离席，关掉页面会由笨机托管"}), 409
        remove_seat(t, s, "离开了")
        if not occupied_seats(t["room"]):
            delete_table(t["id"])
            return jsonify({"ok": True, "dissolved": True})
        save_room(t)
        return jsonify({"ok": True})


@app.route("/mj/api/start", methods=["POST"])
def api_start():
    body = request.get_json(silent=True) or {}
    with LOCK:
        t, s, err = room_auth(body)
        if err:
            return jsonify({"ok": False, "error": err[0]}), err[1]
        r = t["room"]
        if t["phase"] != "waiting":
            return jsonify({"ok": False, "error": "已经开局了"}), 409
        if s != r["host_seat"]:
            return jsonify({"ok": False, "error": "只有房主能开局"}), 403
        occ = occupied_seats(r)
        if not all(x.get("ready") for x in occ):
            return jsonify({"ok": False, "error": "还有人没准备"}), 409
        start_room(t)
        save_room(t)
        v = E.view(t["state"], s)
        add_drawn(v, t["state"], s)
        v.update({"ok": True, "table": t["id"], "room": room_public(t, s)})
        return jsonify(v)


# ---------- 网页 ----------

PAGE = """<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover,maximum-scale=1">
<title>川麻 · __NAME__</title>
<style>
:root{
 --fg:#f0eee4;--dim:#c6cbb8;--gold:#e8c46a;--go:#6fae57;--pass:#4a5140;
 --felt-a:#3a8a5a;--felt-b:#256b42;--felt-c:#154d2f;--felt-d:#0c3820;
}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{height:100%}
body{margin:0;color:var(--fg);overflow:hidden;
 font:15px/1.45 -apple-system,"PingFang SC",system-ui,sans-serif;
 -webkit-text-size-adjust:100%;background:#0c3820}
#app{position:fixed;inset:0;display:flex;flex-direction:column;
 padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}

/* ---- 顶栏 ---- */
#top{display:flex;align-items:center;gap:6px;padding:6px 10px;flex:0 0 auto;
 background:rgba(0,0,0,.28);font-size:12px;color:var(--dim)}
#top .chip{background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.12);
 border-radius:20px;padding:2px 9px;white-space:nowrap}
#top .grow{flex:1}
#mute{background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.15);color:var(--fg);
 border-radius:50%;width:32px;height:32px;font-size:16px;line-height:1;flex:0 0 auto}

/* ---- 牌桌绿毡 ---- */
#table{flex:1 1 auto;position:relative;min-height:0;
 background:radial-gradient(ellipse 78% 62% at 50% 44%,
   var(--felt-a) 0%,var(--felt-b) 40%,var(--felt-c) 74%,var(--felt-d) 100%);
 box-shadow:inset 0 0 90px rgba(0,0,0,.55)}
#table::before{content:"";position:absolute;inset:0;pointer-events:none;opacity:.07;
 mix-blend-mode:overlay;
 background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='140' height='140'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/><feColorMatrix type='saturate' values='0'/></filter><rect width='140' height='140' filter='url(%23n)'/></svg>")}

/* 三家（对/左/右）容器 */
.seat{position:absolute;display:flex;flex-direction:column;align-items:center;gap:3px;z-index:2}
#seatTop{top:6px;left:50%;transform:translateX(-50%);max-width:84%}
#seatLeft{left:4px;top:50%;transform:translateY(-50%);align-items:flex-start;max-width:23%}
#seatRight{right:4px;top:50%;transform:translateY(-50%);align-items:flex-end;max-width:23%}
#seatLeft .pill,#seatRight .pill{flex-wrap:wrap;max-width:100%;white-space:normal;row-gap:2px}

/* 名牌 */
.pill{display:flex;align-items:center;gap:5px;background:rgba(0,0,0,.42);
 border:1px solid rgba(255,255,255,.16);border-radius:20px;padding:3px 9px;font-size:12px;white-space:nowrap}
.pill .dir{display:inline-flex;align-items:center;justify-content:center;width:18px;height:18px;
 border-radius:50%;background:rgba(255,255,255,.14);font-size:11px;font-weight:700}
.pill.turn{border-color:var(--gold);color:#fff;animation:breathe 1.5s ease-in-out infinite}
.pill .cnt{color:var(--dim);font-size:11px}
@keyframes breathe{0%,100%{box-shadow:0 0 2px 0 rgba(232,196,106,.5)}
 50%{box-shadow:0 0 13px 3px rgba(232,196,106,.6)}}

/* 牌背 */
.backs{display:flex;gap:3px;flex-wrap:wrap;justify-content:center}
.backs.vert{flex-direction:column}
.back{background:linear-gradient(135deg,#2c7a52,#12492e);border:1px solid #0b3620;
 border-radius:3px;box-shadow:inset 0 1px 0 rgba(255,255,255,.18)}
.backs .back{width:16px;height:24px}
.backs.vert .back{width:22px;height:13px}
.omeld{display:flex;gap:3px;flex-wrap:wrap;justify-content:center;font-size:15px;margin-top:1px}
.omeld .g{background:rgba(232,196,106,.16);border:1px solid rgba(232,196,106,.4);
 border-radius:4px;padding:0 3px;line-height:1.5}
.ohu{color:var(--gold);font-weight:700;font-size:12px}

/* ---- 弃牌河 ---- */
/* 弃牌河：四块固定尺寸的区，各放得下 18 张（一家最多约 16 张弃牌：108-52 发牌=56 摸 ÷4）。
   上下家 6 列×3 行，侧家 3 列×6 行；原来的 grid 版侧家只有一列宽，第二列被截。 */
/* 环形：上河(6×3) / [左河(4×4) 中 右河(4×4)] / 下河(6×3)，竖向叠放绝不重叠；整环在牌桌里居中略偏下，顶上让给对家。
   牌大小随屏宽：--rt = 24~30px（390 宽 ≈ 29px）。每张牌是固定 1.1em×1.3em 的盒子，字符在盒内水平垂直居中。 */
#table{--rt:clamp(24px,7.4vw,30px);--tw:calc(1.1*var(--rt));--th:calc(1.3*var(--rt));
 --ringH:calc(10*var(--th) + 18px);--ringTop:calc(50% + 28px - var(--ringH)/2);
 --rowH:calc(3*var(--th) + 4px);--sideH:calc(4*var(--th) + 6px);--sideW:calc(4*var(--tw) + 6px)}
#river{position:absolute;inset:0;z-index:1}
.zone{position:absolute;display:flex;flex-wrap:wrap;gap:2px;align-content:flex-start;overflow:visible}
#zTop{top:var(--ringTop);left:50%;transform:translateX(-50%);width:calc(6*var(--tw) + 10px);height:var(--rowH);justify-content:center}
#zLeft{left:28px;top:calc(var(--ringTop) + var(--rowH) + 6px);width:var(--sideW);height:var(--sideH);
 flex-direction:column;align-content:flex-start}
#zRight{right:28px;top:calc(var(--ringTop) + var(--rowH) + 6px);width:var(--sideW);height:var(--sideH);
 flex-direction:column;align-content:flex-end}
#zBot{top:calc(var(--ringTop) + var(--rowH) + var(--sideH) + 12px);left:50%;transform:translateX(-50%);
 width:calc(6*var(--tw) + 10px);height:var(--rowH);justify-content:center}
#zMid{position:absolute;left:50%;top:calc(var(--ringTop) + var(--rowH) + 6px + var(--sideH)/2);
 transform:translate(-50%,-50%);text-align:center;font-size:12px;color:var(--dim);padding:2px;
 width:calc(100% - 2*(28px + var(--sideW)) - 8px);max-width:120px}
.rt{display:inline-flex;align-items:center;justify-content:center;width:var(--tw);height:var(--th);
 font-size:var(--rt);line-height:1;filter:drop-shadow(0 1px 1px rgba(0,0,0,.5));
 background:rgba(253,252,245,.92);color:#111;border-radius:4px;
 padding:var(--gpt,0) var(--gpr,0) var(--gpb,0) var(--gpl,0)}
/* 弃牌河居中（2026-09-26）：麻将字符的"墨迹"在各家字体里并不在字形盒中央（手机实测反馈偏上偏左；桌面 Chromium/WebKit 量得偏下 0.12~0.18em），
   纯 CSS 的 flex 居中只能居中字形盒。页面加载时用 canvas measureText 量出真实墨迹偏移，写进 --gp* 四个变量，
   用非对称内边距把墨迹推回牌框正中。border-box + 固定宽高，牌的尺寸不变。只作用于 .rt（河牌 + 飞行中的影子）。 */
.rt.hot{background:rgba(232,196,106,.95);outline:2px solid #fff3d0}
.pill .que{background:rgba(232,196,106,.22);border:1px solid rgba(232,196,106,.55);color:#f6e2a4;
 border-radius:10px;padding:0 6px;font-size:11px;font-weight:700}
.pill .que.wait{background:rgba(255,255,255,.08);border-color:rgba(255,255,255,.2);color:var(--dim);font-weight:400}
#zMid .claim{color:var(--gold);font-weight:700;font-size:15px}

/* 终局面板 */
#over{position:absolute;inset:0;display:none;align-items:center;justify-content:center;z-index:5;
 background:rgba(6,26,16,.72)}
#over .card{background:#123d26;border:1px solid var(--gold);border-radius:14px;
 padding:16px 20px;max-width:82%;text-align:center;box-shadow:0 10px 40px rgba(0,0,0,.5)}
#over h2{margin:0 0 8px;font-size:17px;color:var(--gold)}
#over .sc{font-size:14px;line-height:1.9}
#over .sc b{color:#fff}

/* ---- 底部：我 ---- */
#mine{flex:0 0 auto;background:linear-gradient(rgba(0,0,0,0),rgba(0,0,0,.3));
 padding:2px 8px calc(6px + env(safe-area-inset-bottom))}
#msg{color:var(--gold);font-size:12px;min-height:16px;text-align:center}
#acts{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin:4px 0}
#acts:empty{margin:0}
button.a{background:var(--go);color:#0b1a0c;border:none;border-radius:10px;
 padding:11px 18px;font-size:16px;font-weight:800;box-shadow:0 3px 8px rgba(0,0,0,.35);
 animation:rise .18s ease-out}
button.a.gray{background:var(--pass);color:var(--fg)}
button.a.hu{background:var(--gold);color:#3a2c00}
@keyframes rise{from{transform:translateY(12px);opacity:0}to{transform:translateY(0);opacity:1}}
#mymeld{display:flex;gap:4px;flex-wrap:wrap;justify-content:flex-end;margin:2px 0;min-height:0}
#mymeld .g{background:rgba(232,196,106,.16);border:1px solid rgba(232,196,106,.4);
 border-radius:5px;padding:2px 5px;font-size:15px;color:#f4ecd4}
#mymeld .g b{color:var(--gold);font-size:11px;font-weight:700;margin-right:2px}
#hand{display:flex;flex-wrap:wrap;gap:5px;justify-content:center}
.t{background:linear-gradient(#fdfcf5,#efe8d3);color:#1a1a1a;border:none;border-radius:8px;
 min-width:46px;height:62px;padding:4px 3px 3px;display:flex;flex-direction:column;
 align-items:center;justify-content:center;
 box-shadow:0 3px 0 #b7ad92,0 4px 7px rgba(0,0,0,.4)}
/* 大字符放在固定高度的盒子里居中，小标签和它拉开 5px */
.t .e{font-size:30px;line-height:1;display:flex;align-items:center;justify-content:center;height:34px;width:34px}
.t small{font-size:10px;color:#5a5a5a;margin-top:5px;line-height:1}
.t.on{outline:2px solid var(--gold);transform:translateY(-3px);
 box-shadow:0 6px 0 #b7ad92,0 8px 10px rgba(0,0,0,.45)}
.t.off{opacity:.5}

/* ---- 候场（等人）视图：开局前盖在牌桌上，同一 URL 原地切换 ---- */
#wait{position:fixed;inset:0;z-index:20;display:none;flex-direction:column;background:#0c3820;
 padding-top:env(safe-area-inset-top);padding-bottom:env(safe-area-inset-bottom)}
#wait.on{display:flex}
#whead{flex:0 0 auto;padding:10px 14px 8px;background:rgba(0,0,0,.28)}
#whead .row1{display:flex;align-items:center;gap:8px}
#whead .leave{color:var(--dim);font-size:13px;background:none;border:none;padding:4px 6px 4px 0}
#whead .code{font-size:26px;font-weight:800;letter-spacing:3px;color:var(--gold);flex:1;text-align:center}
#whead .copy{background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.18);color:var(--fg);
 border-radius:16px;padding:4px 10px;font-size:12px}
#whead .row2{display:flex;flex-wrap:wrap;gap:5px;justify-content:center;margin-top:7px}
#whead .row2 .chip{background:rgba(0,0,0,.3);border:1px solid rgba(255,255,255,.12);color:var(--dim);
 border-radius:20px;padding:2px 9px;font-size:11px}
#wfelt{flex:1 1 auto;position:relative;min-height:0;
 background:radial-gradient(ellipse 78% 62% at 50% 44%,var(--felt-a) 0%,var(--felt-b) 40%,var(--felt-c) 74%,var(--felt-d) 100%);
 box-shadow:inset 0 0 90px rgba(0,0,0,.55)}
.ws{position:absolute;width:104px;min-height:96px;background:rgba(0,0,0,.34);border:1px solid rgba(255,255,255,.16);
 border-radius:14px;padding:8px 6px;display:flex;flex-direction:column;align-items:center;gap:3px;text-align:center}
.ws .dir{position:absolute;top:-9px;left:50%;transform:translateX(-50%);background:#123d26;border:1px solid rgba(255,255,255,.25);
 border-radius:10px;padding:0 8px;font-size:11px;color:var(--dim)}
.ws .av{width:38px;height:38px;border-radius:50%;background:rgba(255,255,255,.12);display:flex;align-items:center;
 justify-content:center;font-size:22px;margin-top:4px}
.ws .nm{font-size:13px;font-weight:700;max-width:96px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ws .st{font-size:11px;color:var(--dim)}
.ws .st.ok{color:#9be07a}
.ws .rel{font-size:10px;color:var(--gold);opacity:.85}
.ws.me{border-color:var(--gold)}
.ws.empty{border-style:dashed;color:var(--dim);cursor:pointer}
.ws.empty .av{background:rgba(255,255,255,.06);font-size:18px}
.ws.ready{box-shadow:0 0 12px rgba(155,224,122,.35)}
.wpos0{bottom:12px;left:50%;transform:translateX(-50%)}
.wpos1{right:8px;top:50%;transform:translateY(-50%)}
.wpos2{top:16px;left:50%;transform:translateX(-50%)}
.wpos3{left:8px;top:50%;transform:translateY(-50%)}
#wmid{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;color:var(--dim);
 font-size:13px;line-height:1.5;max-width:44%}
#wmid b{display:block;color:var(--gold);font-size:18px}
#wfoot{flex:0 0 auto;padding:8px 12px 10px;background:linear-gradient(rgba(0,0,0,0),rgba(0,0,0,.35));
 display:flex;flex-direction:column;gap:7px}
#wmsg{color:var(--gold);font-size:12px;min-height:16px;text-align:center}
#wfoot .big{width:100%;background:var(--go);color:#0b1a0c;border:none;border-radius:12px;padding:13px;
 font-size:17px;font-weight:800;box-shadow:0 3px 8px rgba(0,0,0,.35)}
#wfoot .big.off{background:var(--pass);color:var(--fg)}
#wfoot .big.host{background:var(--gold);color:#3a2c00}
#wfoot .big:disabled{opacity:.45}
#wfoot .row{display:flex;gap:7px}
#wfoot .row button{flex:1;background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.18);color:var(--fg);
 border-radius:10px;padding:9px 4px;font-size:13px}
#wlog{font-size:11px;color:var(--dim);text-align:center;opacity:.8;min-height:14px}
/* ---- 对家牌背一行放下（2026-09-26）：绝对定位 + left:50% 时 shrink-to-fit 只有半桌宽，13 张被挤成两行 ---- */
#seatTop{width:max-content}
#seatTop .backs{flex-wrap:nowrap;gap:3px}
#seatTop .backs .back{--bw:min(16px,calc((84vw - 42px)/14));width:var(--bw);height:calc(1.5*var(--bw))}

/* ---- 手感：抬牌 / 落河 / 高亮 / 碰杠 / 胡字（2026-09-26）---- */
.t{transition:transform .12s ease-out,box-shadow .12s ease-out}
.t.on.lift{transform:translateY(-10px) scale(1.06);outline:2px solid #fff3d0;
 box-shadow:0 9px 0 #b7ad92,0 14px 16px rgba(0,0,0,.5),0 0 12px 2px rgba(232,196,106,.75)}
.t.gone{visibility:hidden}
.rt.fz-me{background:#fff3cf;box-shadow:0 0 0 2px #e8c46a,0 0 9px 1px rgba(232,196,106,.85);animation:fzMe 1.5s linear forwards}
.rt.fz-ot{box-shadow:0 0 0 2px #fff,0 0 8px 1px rgba(255,255,255,.65);animation:fzOt 1.5s linear forwards}
@keyframes fzMe{0%,78%{box-shadow:0 0 0 2px #e8c46a,0 0 9px 1px rgba(232,196,106,.85)}100%{box-shadow:0 0 0 0 rgba(232,196,106,0),0 0 0 0 rgba(232,196,106,0)}}
@keyframes fzOt{0%,78%{box-shadow:0 0 0 2px #fff,0 0 8px 1px rgba(255,255,255,.65)}100%{box-shadow:0 0 0 0 rgba(255,255,255,0),0 0 0 0 rgba(255,255,255,0)}}
.fz-ghost{position:fixed;z-index:50;pointer-events:none;margin:0;will-change:transform}
#mymeld .g i,.omeld .g i{font-style:normal;display:inline-block}
#fzBig{position:fixed;inset:0;z-index:40;display:flex;flex-direction:column;align-items:center;justify-content:center;
 pointer-events:none;text-align:center;
 background:radial-gradient(ellipse 70% 32% at 50% 50%,rgba(6,26,16,.82) 0%,rgba(6,26,16,.55) 55%,rgba(6,26,16,0) 100%)}
#fzBig[hidden]{display:none}
#fzBig b{font:900 92px/1 -apple-system,"PingFang SC",system-ui,sans-serif;color:var(--gold);letter-spacing:4px;
 text-shadow:0 0 28px rgba(232,196,106,.75),0 4px 0 #6b4c00,0 8px 24px rgba(0,0,0,.6)}
#fzBig small{margin-top:10px;font-size:16px;color:#fff;text-shadow:0 2px 6px rgba(0,0,0,.7)}
#fzBig.ot b{font-size:56px;color:#fff;text-shadow:0 0 18px rgba(255,255,255,.5),0 3px 0 rgba(0,0,0,.35),0 6px 18px rgba(0,0,0,.6)}
#over.hold{visibility:hidden}
/* ---- 观战席（2026-09-26）---- */
.watchnote{width:100%;text-align:center;color:var(--dim);font-size:13px;padding:10px 0 6px}
.watchnote b{color:var(--gold);font-weight:800;margin-right:6px}
#wlink{background:rgba(0,0,0,.35);border:1px solid rgba(255,255,255,.15);color:var(--fg);
 border-radius:16px;height:32px;padding:0 9px;font-size:12px;flex:0 0 auto;white-space:nowrap}
@media (prefers-reduced-motion:reduce){
 .t,.t.on.lift{transition:none}
 .rt.fz-me,.rt.fz-ot{animation:none}
}
</style></head><body>
<div id="wait">
 <div id="whead">
  <div class="row1"><button class="leave" onclick="leaveRoom()">‹ 离开</button>
   <span class="code" id="wcode"></span>
   <button class="copy" onclick="copyText(TABLE,this,'已复制')">复制房号</button></div>
  <div class="row2" id="wchips"></div>
 </div>
 <div id="wfelt"><div id="wmid"></div></div>
 <div id="wfoot">
  <div id="wmsg"></div>
  <button class="big" id="wready" onclick="toggleReady()">准 备</button>
  <button class="big host" id="wstart" onclick="startGame()" style="display:none">开局（空位笨机补）</button>
  <div class="row">
   <button onclick="shareRoom(this)">分享链接</button>
   <button onclick="inviteBot(this)">叫我的小机来</button>
   <button onclick="copyWatch(this)">围观链接</button>
  </div>
  <div id="wlog"></div>
 </div>
</div>
<div id="app">
 <div id="top">
   <span class="chip" id="cWho"></span>
   <span class="chip" id="cWall"></span>
   <span class="chip" id="cPhase"></span>
   <span class="grow"></span>
   <button id="wlink" onclick="copyWatch(this)" title="复制围观链接">👁 围观</button>
   <button id="mute" onclick="toggleMute()" title="音效（含震动）" aria-label="音效开关">🔊</button>
 </div>
 <div id="table">
   <div class="seat" id="seatTop"></div>
   <div class="seat" id="seatLeft"></div>
   <div class="seat" id="seatRight"></div>
   <div id="river">
     <div class="zone" id="zTop"></div>
     <div class="zone" id="zLeft"></div>
     <div id="zMid"></div>
     <div class="zone" id="zRight"></div>
     <div class="zone" id="zBot"></div>
   </div>
   <div id="over"><div class="card"><h2 id="overT"></h2><div class="sc" id="overS"></div></div></div>
 </div>
 <div id="mine">
   <div id="msg"></div>
   <div id="acts"></div>
   <div id="mymeld"></div>
   <div id="hand"></div>
 </div>
</div>
<script>
/* ---------- 手感层（2026-09-26）：抬牌→落河、河里高亮、碰杠飞牌、胡字停顿、WebAudio 合成音、震动。
   全部包在这个 IIFE 里，只暴露 MJFeel；主脚本在 apply() 里调 pre/post。开关 = 顶栏 🔊（mj_mute，音效+震动同一个）。 ---------- */
const MJFeel=(()=>{
  const mq=window.matchMedia?window.matchMedia('(prefers-reduced-motion: reduce)'):null;
  const RM=()=>!!(mq&&mq.matches);
  const canAnim=el=>el&&typeof el.animate==='function'&&!RM();
  const now=()=>performance.now();
  const muted=()=>{ try{ return SFX.get(); }catch(e){ return false; } };
  const stats={osc:0,sounds:[]};
  const FRESH_MS=1500;
  let fresh={};            // seat -> {i,t0,me}：河里刚打出的那张
  let lifted=null;         // {i,t}：抬起的手牌
  let flight=null;         // {tile}：自己那张正在飞
  let selfAt=-1e9;         // 上次自己出牌的时刻（压掉 sfxDiff 的重复「啪」和重复落地）
  let holdUntil=0;         // 动画进行中：轮询先别重画

  /* ---- 音频：首次手势里 resume + 静音 buffer 解锁（iOS Safari），之后全用合成 ---- */
  let ctx=null, master=null, noiseBuf=null, primed=false;
  function ac(){
    if(ctx) return ctx;
    const C=window.AudioContext||window.webkitAudioContext; if(!C) return null;
    try{ ctx=new C(); master=ctx.createGain(); master.gain.value=0.9; master.connect(ctx.destination); }catch(e){ ctx=null; }
    return ctx;
  }
  function unlock(){
    const c=ac(); if(!c) return;
    if(c.state!=='running'){ try{ const p=c.resume(); if(p&&p.catch) p.catch(()=>{}); }catch(e){} }
    if(!primed){
      try{ const b=c.createBuffer(1,1,22050), s=c.createBufferSource(); s.buffer=b; s.connect(c.destination); s.start(0); primed=true; }catch(e){}
    }
  }
  ['pointerdown','touchend','click','keydown'].forEach(n=>document.addEventListener(n,unlock,{capture:true,passive:true}));
  function live(){ if(muted()) return null; const c=ctx; if(!c) return null;
    if(c.state!=='running'){ try{ c.resume(); }catch(e){} return c.state==='running'?c:null; } return c; }
  function envGain(c,t,peak,att,dec){
    const g=c.createGain(); g.gain.setValueAtTime(0.0001,t);
    g.gain.exponentialRampToValueAtTime(peak,t+att); g.gain.exponentialRampToValueAtTime(0.0001,t+att+dec);
    g.connect(master); return g;
  }
  function osc(c,type,f0,f1,t,dur,g){
    const o=c.createOscillator(); o.type=type; o.frequency.setValueAtTime(f0,t);
    if(f1) o.frequency.exponentialRampToValueAtTime(f1,t+dur*0.6);
    o.connect(g); o.start(t); o.stop(t+dur); stats.osc++; return o;
  }
  function noise(c,t,dur,type,freq,q,g){
    if(!noiseBuf){ const n=Math.floor(c.sampleRate*0.2); noiseBuf=c.createBuffer(1,n,c.sampleRate);
      const d=noiseBuf.getChannelData(0); for(let i=0;i<n;i++) d[i]=Math.random()*2-1; }
    const s=c.createBufferSource(); s.buffer=noiseBuf;
    const f=c.createBiquadFilter(); f.type=type; f.frequency.value=freq; f.Q.value=q;
    s.connect(f); f.connect(g); s.start(t); s.stop(t+dur);
  }
  const SND={
    pa(v){ const c=live(); if(!c) return false; const t=c.currentTime+0.005;       // 啪：≤100ms
      osc(c,'triangle',1250,240,t,0.08,envGain(c,t,0.45*v,0.002,0.07));
      noise(c,t,0.06,'bandpass',2300,0.9,envGain(c,t,0.9*v,0.001,0.045)); return true; },
    peng(v){ const c=live(); if(!c) return false; const t=c.currentTime+0.005;     // 短促双音
      osc(c,'sine',740,0,t,0.09,envGain(c,t,0.35*v,0.004,0.08));
      osc(c,'sine',988,0,t+0.1,0.1,envGain(c,t+0.1,0.35*v,0.004,0.09)); return true; },
    gang(v){ const c=live(); if(!c) return false; const t=c.currentTime+0.005;     // 锣：165Hz 短衰减
      osc(c,'sine',175,160,t,0.62,envGain(c,t,0.7*v,0.006,0.58));
      osc(c,'triangle',455,440,t,0.34,envGain(c,t,0.16*v,0.004,0.3));
      noise(c,t,0.07,'lowpass',420,0.7,envGain(c,t,0.5*v,0.002,0.06)); return true; }
  };
  function sound(n,v){ stats.sounds.push(n); try{ return SND[n](v==null?1:v); }catch(e){ return false; } }
  function vib(ms){ if(muted()) return; try{ if(navigator.vibrate) navigator.vibrate(ms); }catch(e){} }
  // SFX.play 的分流：啪/碰/杠有合成就走合成（碰杠声由落位时的动画负责），没有 AudioContext 才回落到 wav
  function route(n){
    if(!ctx) return false;
    if(n==='discard'){ if(now()-selfAt<900) return true; sound('pa',0.6); return true; }
    if(n==='peng'||n==='gang') return true;
    return false;
  }

  /* ---- DOM 小工具 ---- */
  const riverOf=seat=>document.querySelectorAll('#river .rt[data-s="'+seat+'"]');
  const lastOf=seat=>{ const a=riverOf(seat); return a.length?a[a.length-1]:null; };
  function meldGroup(seat,k){
    if(S&&seat===S.my_seat) return document.querySelectorAll('#mymeld .g')[k]||null;
    const id={1:'seatRight',2:'seatTop',3:'seatLeft'}[rel(seat)];
    const box=id&&document.getElementById(id); if(!box) return null;
    return box.querySelectorAll('.omeld .g')[k]||null;
  }
  function ghostAt(r,tile){
    const g=document.createElement('span'); g.className='rt fz-ghost'; g.textContent=emo(tile);
    g.style.left=r.left+'px'; g.style.top=r.top+'px'; g.style.width=r.width+'px'; g.style.height=r.height+'px';
    document.getElementById('table').appendChild(g); return g;
  }
  const hold=ms=>{ holdUntil=Math.max(holdUntil,now()+ms); };

  /* ---- 河里高亮：riverHTML 每次重画都问一次 ---- */
  function cur(seat,i){ const f=fresh[seat]; return (f&&f.i===i&&now()-f.t0<FRESH_MS)?f:null; }
  function rc(seat,i){ const f=cur(seat,i); return f?(f.me?' fz-me':' fz-ot'):''; }
  function rs(seat,i){ const f=cur(seat,i); return f?' style="animation-delay:-'+Math.round(now()-f.t0)+'ms"':''; }
  function expire(){
    Object.keys(fresh).forEach(seat=>{ const f=fresh[seat]; const left=FRESH_MS-(now()-f.t0);
      if(left<=0){ delete fresh[seat]; }
      setTimeout(()=>{ const el=riverOf(seat)[f.i]; if(el&&!cur(+seat,f.i)) el.classList.remove('fz-me','fz-ot'); },Math.max(0,left)+30);
    });
  }

  /* ---- 抬牌 → 确认 ---- */
  function dropLift(){ lifted=null; document.querySelectorAll('#hand .t.lift').forEach(b=>b.classList.remove('lift')); }
  document.addEventListener('pointerdown',e=>{ if(lifted&&!(e.target.closest&&e.target.closest('#hand'))) dropLift(); },{passive:true});
  function tap(btn,i,tile){
    unlock();
    if(busy) return;
    if(lifted&&lifted.i===i&&lifted.t===tile&&btn.classList.contains('lift')){ lifted=null; confirmDiscard(btn,tile); return; }
    dropLift(); btn.classList.add('lift'); lifted={i,t:tile};
  }
  function confirmDiscard(btn,tile){
    const me=S&&S.players.find(p=>p.is_me);
    selfAt=now(); sound('pa',1); vib(15);
    if(me) fresh[S.my_seat]={i:me.discards.length,t0:now(),me:true};
    const zone=document.getElementById('zBot');
    const ph=document.createElement('span'); ph.className='rt fz-me'; ph.dataset.s=S.my_seat; ph.dataset.t=tile;
    ph.textContent=emo(tile); zone.appendChild(ph);
    if(canAnim(ph)){
      const a=btn.getBoundingClientRect(), b=ph.getBoundingClientRect();
      ph.style.visibility='hidden';
      const g=ghostAt(b,tile);
      const dx=(a.left+a.width/2)-(b.left+b.width/2), dy=(a.top+a.height/2)-(b.top+b.height/2), s=a.width/b.width;
      flight={tile}; hold(420);
      const an=g.animate([
        {transform:'translate('+dx+'px,'+dy+'px) scale('+s+')',filter:'drop-shadow(0 10px 10px rgba(0,0,0,.45))',offset:0},
        {transform:'translate(0,0) scale(1.14)',filter:'drop-shadow(0 6px 6px rgba(0,0,0,.55))',offset:0.62},
        {transform:'translate(0,0) scale(.95)',filter:'drop-shadow(0 2px 3px rgba(0,0,0,.5))',offset:0.82},
        {transform:'translate(0,0) scale(1)',filter:'drop-shadow(0 1px 1px rgba(0,0,0,.5))',offset:1}
      ],{duration:300,easing:'cubic-bezier(.25,.7,.3,1)'});   // 前 ~190ms 是飞，后面是落定回弹
      const land=()=>{ g.remove(); const el=lastOf(S.my_seat); if(el&&el.dataset.t===tile) el.style.visibility=''; flight=null; };
      an.onfinish=land; an.oncancel=land;
    }
    btn.classList.add('gone');
    send('discard',tile);
  }

  /* ---- 状态 diff：pre 在 render 前（量旧 DOM 的源位置），post 在 render 后（放动画） ---- */
  function pre(a,b){
    const ev={disc:[],meld:[],hu:[],over:false};
    if(!a||!b||!a.players||!b.players) return ev;
    const t=now();
    b.players.forEach(p=>{
      const q=a.players.find(x=>x.seat===p.seat); if(!q) return;
      if(p.discards.length>q.discards.length){
        const i=p.discards.length-1, f=fresh[p.seat];
        if(!(f&&f.i===i&&t-f.t0<FRESH_MS)) fresh[p.seat]={i,t0:t,me:!!p.is_me};
        ev.disc.push({seat:p.seat,i,me:!!p.is_me});
      }
      p.melds.forEach((m,k)=>{
        const om=q.melds[k];
        if(om&&om.type===m.type) return;
        if(om&&!(om.type==='peng'&&m.type==='gang_bu')) return;
        let from=null;
        if((m.type==='peng'||m.type==='gang_ming')&&a.last_discard===m.tile&&a.last_discard_seat!=null){
          const src=lastOf(a.last_discard_seat); if(src&&src.dataset.t===m.tile) from=src.getBoundingClientRect();
        }
        ev.meld.push({seat:p.seat,k,type:m.type,tile:m.tile,from,me:!!p.is_me});
      });
      if(p.hu&&!q.hu) ev.hu.push({seat:p.seat,me:!!p.is_me,self:!!p.hu.self_draw,name:p.name});
    });
    ev.over=b.phase==='over'&&a.phase!=='over';
    if(ev.meld.length) hold(1100);
    if(ev.hu.length) hold(800);
    return ev;
  }
  function reLift(){
    if(!lifted) return;
    const b=document.querySelectorAll('#hand .t')[lifted.i];
    if(b&&b.classList.contains('on')&&b.dataset.t===lifted.t) b.classList.add('lift'); else lifted=null;
  }
  function meldFx(m){
    const g=meldGroup(m.seat,m.k); if(!g) return;
    const isGang=m.type.indexOf('gang')===0, v=m.me?1:0.6;
    const finish=()=>{
      g.style.opacity='';
      sound(isGang?'gang':'peng',v); if(m.me) vib(isGang?30:20);
      if(!canAnim(g)) return;
      g.querySelectorAll('i').forEach((el,j)=>el.animate(
        [{opacity:0,transform:'translateX(-7px) scale(.7)'},{opacity:1,transform:'translateX(0) scale(1)'}],
        {duration:190,delay:j*40,easing:'cubic-bezier(.2,.8,.3,1.25)',fill:'backwards'}));
      if(isGang){
        const c=m.me?'0 0 18px 6px rgba(232,196,106,.95)':'0 0 10px 3px rgba(232,196,106,.7)';
        g.animate([{boxShadow:'0 0 0 0 rgba(232,196,106,0)'},{boxShadow:c,offset:0.3},{boxShadow:'0 0 0 0 rgba(232,196,106,0)'}],
          {duration:600,easing:'ease-out'});
      }else if(m.me){
        g.animate([{transform:'scale(1)'},{transform:'scale(1.08)',offset:0.4},{transform:'scale(1)'}],{duration:260,delay:120});
      }
    };
    if(!canAnim(g)){ finish(); return; }
    g.style.opacity='0';
    const tiles=g.querySelectorAll('i'), tgt=tiles.length?tiles[tiles.length-1]:g;
    if(m.from){
      const r=tgt.getBoundingClientRect(), f=m.from;
      const gh=ghostAt(f,m.tile);
      const dx=(r.left+r.width/2)-(f.left+f.width/2), dy=(r.top+r.height/2)-(f.top+f.height/2);
      const s=Math.max(0.45,Math.min(1.2,r.height/f.height)), lift=m.me?46:28, dur=m.me?300:240;
      const an=gh.animate([
        {transform:'translate(0,0) scale(1)',opacity:1},
        {transform:'translate('+dx/2+'px,'+(dy/2-lift)+'px) scale('+((1+s)/2*1.12)+')',opacity:1,offset:0.5},
        {transform:'translate('+dx+'px,'+dy+'px) scale('+s+')',opacity:0.9,offset:1}
      ],{duration:dur,easing:'cubic-bezier(.35,.1,.3,1)'});
      const done=()=>{ gh.remove(); finish(); };
      an.onfinish=done; an.oncancel=done;
    }else{ setTimeout(finish,40); }
  }
  function bigWord(h,ev){
    let el=document.getElementById('fzBig');
    if(!el){ el=document.createElement('div'); el.id='fzBig'; el.hidden=true; document.body.appendChild(el); }
    el.className=h.me?'':'ot';
    el.innerHTML='<b>'+(h.self?'自摸！':'胡！')+'</b>'+(h.me?'':'<small>'+h.name+'</small>');
    el.hidden=false;
    const ov=document.getElementById('over');
    if(ev.over&&ov) ov.classList.add('hold');
    const dur=700;
    if(canAnim(el)){
      el.animate([
        {opacity:0,filter:'blur(12px)',transform:'scale(1.35)'},
        {opacity:1,filter:'blur(0px)',transform:'scale(1)',offset:0.32},
        {opacity:1,filter:'blur(0px)',transform:'scale(1.02)',offset:0.82},
        {opacity:0,filter:'blur(4px)',transform:'scale(1.06)'}
      ],{duration:dur,easing:'ease-out'});
    }
    setTimeout(()=>{
      el.hidden=true;
      if(ov&&ov.classList.contains('hold')){ ov.classList.remove('hold');
        if(canAnim(ov)) ov.animate([{opacity:0},{opacity:1}],{duration:220,easing:'ease-out'}); }
    },dur);
  }
  function post(ev){
    if(flight){ const el=lastOf(S.my_seat); if(el&&el.dataset.t===flight.tile) el.style.visibility='hidden'; }
    reLift(); expire();
    if(!ev) return;
    ev.disc.forEach(d=>{
      if(d.me&&now()-selfAt<2500) return;          // 自己那张已经飞过了
      const el=riverOf(d.seat)[d.i];
      if(canAnim(el)) el.animate([{transform:'scale(1.3)',opacity:0},{transform:'scale(.95)',opacity:1,offset:0.7},
        {transform:'scale(1)',opacity:1}],{duration:200,easing:'ease-out'});
    });
    ev.meld.forEach(meldFx);
    if(ev.hu.length){ const h=ev.hu.find(x=>x.me)||ev.hu[0]; bigWord(h,ev); }
  }
  return {tap,pre,post,rc,rs,route,unlock,holding:()=>now()<holdUntil,
    stats,ctxState:()=>ctx?ctx.state:'none'};
})();
</script>

<script>
const TABLE="__TABLE__", TOKEN="__TOKEN__", WATCH="__WATCH__";   // WATCH 非空 = 观战页（只读）
let S=null, busy=false, actSeq=0;
const MELD={peng:"碰",gang_ming:"明杠",gang_an:"暗杠",gang_bu:"补杠"};
const SUIT={w:"万",o:"筒",t:"条"};
const SEATN=["东","南","西","北"];   // 观战页用绝对座位名（没有"我"，相对方位没意义）
const DIRS=["南","西","北","东"];   // 相对方位：自己永远在南(底)，下家西(右)、对家北(上)、上家东(左)
const EMO={
 w:['','🀇','🀈','🀉','🀊','🀋','🀌','🀍','🀎','🀏'],
 o:['','🀙','🀚','🀛','🀜','🀝','🀞','🀟','🀠','🀡'],
 t:['','🀐','🀑','🀒','🀓','🀔','🀕','🀖','🀗','🀘']};
function emo(t){ return EMO[t[1]][+t[0]]; }
function lbl(t){ return t[0]+SUIT[t[1]]; }

/* ---------- 音效层（占位音，加载失败静默降级，不阻塞逻辑） ---------- */
const SFX=(()=>{
  const names=['draw','discard','peng','gang','hu','turn'];
  const cache={};
  names.forEach(n=>{ try{ const a=new Audio('/mj/static/sfx/'+n+'.wav'); a.preload='auto'; cache[n]=a; }catch(e){} });
  let muted=false; try{ muted=localStorage.getItem('mj_mute')==='1'; }catch(e){}
  window.__sfxLog=[];
  function play(n){
    window.__sfxLog.push(n);
    if(muted) return;
    try{ if(MJFeel.route(n)) return; }catch(e){}
    const a=cache[n]; if(!a) return;
    try{ a.currentTime=0; const p=a.play(); if(p&&p.catch) p.catch(()=>{}); }catch(e){}
  }
  return {play,
    set:(m)=>{muted=m; try{localStorage.setItem('mj_mute',m?'1':'0');}catch(e){}},
    get:()=>muted};
})();
function toggleMute(){ SFX.set(!SFX.get()); MJFeel.unlock(); document.getElementById('mute').textContent=SFX.get()?'🔇':'🔊'; }

function sfxDiff(a,b){
  if(!a||!b) return;
  const gangc=s=>s.players.reduce((n,p)=>n+p.melds.filter(m=>m.type.indexOf('gang')===0).length,0);
  const pengc=s=>s.players.reduce((n,p)=>n+p.melds.filter(m=>m.type==='peng').length,0);
  const huc=s=>s.players.filter(p=>p.hu).length;
  const meT=s=>{const m=s.players.find(p=>p.is_me);return m&&m.is_turn;};
  const meH=s=>{const m=s.players.find(p=>p.is_me);return m?m.hand_count:0;};
  if(gangc(b)>gangc(a)) SFX.play('gang');
  else if(pengc(b)>pengc(a)) SFX.play('peng');
  if(huc(b)>huc(a)) SFX.play('hu');
  if(meT(b)&&!meT(a)){ if(meH(b)>meH(a)) SFX.play('draw'); SFX.play('turn'); }
  else if(b.last_discard && b.last_discard!==a.last_discard) SFX.play('discard');
}

async function pull(){
  if(busy || MJFeel.holding()) return;
  const seq=actSeq;
  try{
    const r=await fetch(WATCH?`/mj/api/state?table=${TABLE}&watch=${WATCH}`:`/mj/api/state?table=${TABLE}&token=${TOKEN}`);
    const j=await r.json();
    if(busy || seq!==actSeq) return;     // 出手期间发出的轮询作废，免得旧状态盖住新状态/打断动画
    if(!j.ok){
      if(j.gone){ location.href='/mj/?gone=1'; return; }
      document.getElementById('msg').textContent=j.error; document.getElementById('wmsg').textContent=j.error; return; }
    apply(j);
  }catch(e){}
}

/* ---------- 候场 ---------- */
const WAIT=document.getElementById('wait');
async function roomPost(path,body){
  const r=await fetch('/mj/api/'+path,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(Object.assign({room:TABLE,token:TOKEN},body||{}))});
  return r.json();
}
async function copyText(text,btn,done){
  const old=btn?btn.textContent:'';
  try{ await navigator.clipboard.writeText(text); }catch(e){ window.prompt('复制这段：',text); }
  if(btn){ btn.textContent=done||'已复制 ✓'; setTimeout(()=>btn.textContent=old,1500); }
}
function roomLink(){ return location.origin+'/mj/r/'+TABLE; }
let watchURL=WATCH?location.origin+'/mj/w/'+TABLE+'/'+WATCH:'';
async function copyWatch(btn){
  if(!watchURL){
    try{
      const r=await fetch('/mj/api/watch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({room:TABLE})});
      const j=await r.json();
      if(!j.ok){ document.getElementById('msg').textContent=j.error; document.getElementById('wmsg').textContent=j.error; return; }
      watchURL=location.origin+j.watch_url;
    }catch(e){ return; }
  }
  copyText(`神庭麻将 ${TABLE} 房，来围观（只看不打，看不到任何人的手牌）：${watchURL}`,btn,'已复制围观链接');
}
async function shareRoom(btn){
  const text=`来神庭麻将打一局，房号 ${TABLE}：${roomLink()}`;
  if(navigator.share){ try{ await navigator.share({title:'神庭麻将',text}); return; }catch(e){} }
  copyText(text,btn,'已复制邀请');
}
function inviteBot(btn){
  const me=(S&&S.room&&S.room.seats[S.my_seat])||{};
  copyText(`来神庭麻将 ${TABLE} 房，我（${me.name||S.my_name}）在${me.dir||''}座。加入接口：POST ${location.origin}/mj/api/join {"room":"${TABLE}","nick":"你的名字","kind":"machine"}，然后轮询返回的 state_url。`,btn,'已复制，发给小机');
}
async function toggleReady(){
  const me=S.room.seats[S.my_seat];
  const j=await roomPost('ready',{ready:me.state!=='ready'});
  if(!j.ok){ document.getElementById('wmsg').textContent=j.error; return; }
  S.room=j.room; renderWait(); pull();
}
async function sitAt(seat){
  const j=await roomPost('sit',{seat});
  if(!j.ok){ document.getElementById('wmsg').textContent=j.error; return; }
  S.my_seat=j.seat; S.room=j.room; renderWait();
}
async function startGame(){
  const b=document.getElementById('wstart'); b.disabled=true;
  const j=await roomPost('start');
  if(!j.ok){ document.getElementById('wmsg').textContent=j.error; b.disabled=false; return; }
  apply(j);
}
async function leaveRoom(){
  if(WATCH){ location.href='/mj/'; return; }
  if(!confirm('离开这一桌？')) return;
  await roomPost('leave'); location.href='/mj/';
}
function seatHTML(s,pos,r){
  const isMe=!WATCH&&s.seat===S.my_seat;
  const cls=['ws','wpos'+pos, isMe?'me':'', s.state==='empty'?'empty':'', s.state==='ready'?'ready':''].join(' ');
  if(s.state==='empty'){
    if(WATCH) return `<div class="${cls}"><span class="dir">${s.dir}</span><div class="av">◯</div><div class="nm">空座</div><div class="st">等人来</div></div>`;
    return `<div class="${cls}" onclick="sitAt(${s.seat})"><span class="dir">${s.dir}</span><div class="av">◯</div><div class="nm">空座</div><div class="st">点我换到这</div></div>`;
  }
  if(s.state==='reserved'){
    return `<div class="${cls}"><span class="dir">${s.dir}</span><div class="av">⏳</div><div class="nm">留给 ${s.for_name}</div><div class="st">${s.left}s 后放开</div></div>`;
  }
  const av=s.kind==='machine'?'🕯️':(s.kind==='bot'?'🤖':'👤');
  const st=s.state==='ready'?'<div class="st ok">已准备 ✓</div>':'<div class="st">还没准备</div>';
  const host=s.is_host?' 👑':'';
  const rel=s.partner_of?`<div class="rel">${s.partner_of} 的灵伴</div>`:'';
  return `<div class="${cls}"><span class="dir">${s.dir}</span><div class="av">${av}</div><div class="nm">${s.name}${host}${isMe?'（我）':''}</div>${st}${rel}</div>`;
}
function renderWait(){
  const r=S.room;
  WAIT.classList.add('on');
  document.getElementById('wcode').textContent=TABLE;
  const st=r.settings||{};
  document.getElementById('wchips').innerHTML=
    [`${st.mode||'血战到底'} · ${st.players||4} 人`,`${st.rounds||1} 局`,...(st.rule||[]),r.visible?'公开房':'仅房号可进',r.name]
    .map(c=>`<span class="chip">${c}</span>`).join('');
  let html='';
  r.seats.forEach(s=>{ html+=seatHTML(s,rel(s.seat),r); });
  const mid = r.countdown_left!=null ? `<b>${r.countdown_left} 秒后开局</b>四座齐了`
    : (r.need>0 ? `<b>${r.occupied}/4</b>还差 ${r.need} 人<br><small>空座开局由笨机补</small>` : `<b>4/4</b>等大家准备`);
  document.getElementById('wfelt').innerHTML=html+`<div id="wmid">${mid}</div>`;
  if(WATCH){
    document.getElementById('wready').style.display='none';
    document.getElementById('wstart').style.display='none';
    document.querySelector('#whead .leave').textContent='‹ 大厅';
    document.querySelectorAll('#wfoot .row button').forEach(b=>{ if(!/围观/.test(b.textContent)) b.style.display='none'; });
    document.getElementById('wmsg').textContent=`👁 观战中${r.watching?' · '+r.watching+' 人在看':''} · 开局后自动切到牌桌`;
    document.getElementById('wlog').textContent=(r.log||[]).slice(-2).join(' · ');
    return;
  }
  const me=r.seats[S.my_seat]||{};
  const rb=document.getElementById('wready');
  rb.textContent = me.state==='ready' ? '取消准备' : '准 备';
  rb.className = 'big'+(me.state==='ready'?' off':'');
  const sb=document.getElementById('wstart');
  sb.style.display = r.is_host ? '' : 'none';
  sb.disabled = !r.can_start;
  sb.textContent = r.need>0 ? `开局（${r.need} 个空座笨机补）` : '开局';
  document.getElementById('wlog').textContent=(r.log||[]).slice(-2).join(' · ');
}
function rid(){ return Date.now().toString(36)+'-'+Math.random().toString(36).slice(2,10); }
async function send(action,tile){
  if(busy) return; busy=true; actSeq++;
  document.getElementById('msg').textContent="…";
  // 幂等：同一 request_id + 上次看到的 fp；响应丢了自动重发一次，服务端不会重复出手
  const body=JSON.stringify({table:TABLE,token:TOKEN,action,tile,request_id:rid(),fp:S&&S.fp});
  let done=false;
  for(let attempt=0; attempt<2 && !done; attempt++){
    try{
      const r=await fetch('/mj/api/act',{method:'POST',headers:{'Content-Type':'application/json'},body});
      const j=await r.json();
      done=true;
      if(!j.ok){ document.getElementById('msg').textContent="✗ "+j.error; try{ render(); }catch(e){} }
      else { document.getElementById('msg').textContent=""; apply(j); }
    }catch(e){
      if(attempt===0){ document.getElementById('msg').textContent="网络卡了，重试中…"; await new Promise(r=>setTimeout(r,700)); }
      else { document.getElementById('msg').textContent="网络卡了，再点一次"; try{ render(); }catch(e){} }
    }
  }
  busy=false;
}
function apply(j){
  if(j.phase==='waiting'){ S=j; renderWait(); return; }
  const wasWaiting = WAIT.classList.contains('on');
  WAIT.classList.remove('on');
  const old=(S&&S.phase==='waiting')?null:S; S=j;
  try{ if(wasWaiting) SFX.play('turn'); else sfxDiff(old,j); }catch(e){}
  let ev=null; try{ ev=MJFeel.pre(old,j); }catch(e){}
  render();
  try{ MJFeel.post(ev); }catch(e){}
}

function rel(seat){ return ((seat - S.my_seat)%4+4)%4; }   // 0我 1右 2上 3左

/* 弃牌河字符墨迹居中：量一次，按 em 写进 #table 的 --gp*（见 .rt 的 CSS 注释） */
function centerRiverGlyphs(){
  try{
    const host=document.getElementById('river'); if(!host) return;
    const probe=document.createElement('span'); probe.className='rt';
    probe.style.cssText='position:absolute;left:0;top:0;visibility:hidden;padding:0';
    const inner=document.createElement('span'), mark=document.createElement('i');
    mark.style.cssText='display:inline-block;width:0;height:0;vertical-align:baseline';
    probe.appendChild(inner); host.appendChild(probe);
    const cs=getComputedStyle(probe), fs=parseFloat(cs.fontSize)||1;
    const c=document.createElement('canvas').getContext('2d');
    c.font=cs.fontStyle+' '+cs.fontWeight+' '+cs.fontSize+' '+cs.fontFamily; c.textBaseline='alphabetic'; c.textAlign='left';
    const glyphs=['🀇','🀙','🀐','🀋','🀝','🀔'];
    let sx=0, sy=0, n=0;
    glyphs.forEach(g=>{
      inner.textContent=g; inner.appendChild(mark);
      const m=c.measureText(g);
      if(m.actualBoundingBoxAscent==null||m.actualBoundingBoxRight==null) return;
      const box=probe.getBoundingClientRect(), gl=inner.getBoundingClientRect().left, base=mark.getBoundingClientRect().bottom;
      const inkL=gl-m.actualBoundingBoxLeft, inkR=gl+m.actualBoundingBoxRight;
      const inkT=base-m.actualBoundingBoxAscent, inkB=base+m.actualBoundingBoxDescent;
      if(!(inkR>inkL&&inkB>inkT)) return;
      sx+=(inkL+inkR)/2-(box.left+box.width/2); sy+=(inkT+inkB)/2-(box.top+box.height/2); n++;
    });
    host.removeChild(probe);
    if(!n) return;
    const dx=sx/n/fs, dy=sy/n/fs;               // 墨迹中心相对牌框中心的偏移（em；正=偏右/偏下）
    if(Math.abs(dx)>0.4||Math.abs(dy)>0.4) return;  // 量得离谱就别动
    const T=document.getElementById('table').style;
    const em=v=>(Math.max(0,v)*2).toFixed(3)+'em';
    T.setProperty('--gpl',em(-dx)); T.setProperty('--gpr',em(dx));
    T.setProperty('--gpt',em(-dy)); T.setProperty('--gpb',em(dy));
    window.__rtCenter={dx:+dx.toFixed(3),dy:+dy.toFixed(3),n};
  }catch(e){}
}
centerRiverGlyphs();
try{ if(document.fonts&&document.fonts.ready) document.fonts.ready.then(()=>{ const T=document.getElementById('table').style;
  ['--gpl','--gpr','--gpt','--gpb'].forEach(k=>T.removeProperty(k)); centerRiverGlyphs(); }); }catch(e){}

function oppHTML(p){
  const turn=p.is_turn?'turn':'';
  const hu=p.hu?`<div class="ohu">🎉${(p.hu.flags||[]).join('/')} ${p.hu.fan}番</div>`:'';
  const backs=Array(p.hand_count).fill('<div class="back"></div>').join('');
  const vert=(rel(p.seat)!==2)?'vert':'';
  const mel=p.melds.map(m=>`<span class="g">${m.tiles.map(x=>`<i>${emo(x)}</i>`).join('')}</span>`).join('');
  const que = p.que ? `<span class="que">缺${SUIT[p.que]}</span>`
    : (S.phase=='que' ? `<span class="que wait">定缺中</span>` : '');
  return `<div class="pill ${turn}"><span class="dir">${WATCH?SEATN[p.seat]:DIRS[rel(p.seat)]}</span>${p.name}`+
    `${p.type=='bot'?' 🤖':(p.type=='api'?' 🕯️':'')} <span class="cnt">${p.hand_count}</span>${que}</div>`+
    `${mel?`<div class="omeld">${mel}</div>`:''}`+
    `<div class="backs ${vert}">${backs}</div>${hu}`;
}
function riverHTML(p){
  const hot=(t,i)=> (p.seat===S.last_discard_seat && i===p.discards.length-1 && S.last_discard)?'hot':'';
  return p.discards.map((t,i)=>`<span class="rt ${hot(t,i)}${MJFeel.rc(p.seat,i)}" data-s="${p.seat}" data-t="${t}"${MJFeel.rs(p.seat,i)}>${emo(t)}</span>`).join('');
}

function render(){
  const me=WATCH?S.players[S.my_seat]:S.players.find(p=>p.is_me);   // 观战：底边那家
  document.getElementById('cWho').textContent=WATCH?'👁 观战':('🀄️ '+S.my_name+(S.my_que?`·缺${SUIT[S.my_que]}`:''));
  document.getElementById('cWall').textContent='墙 '+S.wall;
  document.getElementById('cPhase').textContent=
    S.phase=='que'?'定缺中':(S.phase=='over'?'本局结束':(S.step=='claim'?'抢牌中':'进行中'));
  document.getElementById('mute').textContent=SFX.get()?'🔇':'🔊';

  // 三家
  const byRel={};
  S.players.forEach(p=>{ byRel[rel(p.seat)]=p; });
  document.getElementById('seatTop').innerHTML  = byRel[2]?oppHTML(byRel[2]):'';
  document.getElementById('seatRight').innerHTML= byRel[1]?oppHTML(byRel[1]):'';
  document.getElementById('seatLeft').innerHTML = byRel[3]?oppHTML(byRel[3]):'';

  // 河
  document.getElementById('zTop').innerHTML  = byRel[2]?riverHTML(byRel[2]):'';
  document.getElementById('zRight').innerHTML= byRel[1]?riverHTML(byRel[1]):'';
  document.getElementById('zLeft').innerHTML = byRel[3]?riverHTML(byRel[3]):'';
  document.getElementById('zBot').innerHTML  = me?riverHTML(me):'';
  const turnName=(S.players[S.turn]||{}).name||'';
  const queWait=(S.waiting_seats||[]).map(i=>(S.players[i]||{}).name).filter(Boolean);
  document.getElementById('zMid').innerHTML = S.claim_tile
    ? `<span class="claim">抢 ${lbl(S.claim_tile)}</span>`
    : (S.phase=='play'?`轮到<br>${turnName}`
      : (S.phase=='que'?`等定缺<br>${queWait.join('、')}`:''));

  // 动作按钮
  const acts=S.available_actions||[];
  let canDiscard=null, btns=[];
  for(const a of acts){
    if(a.action=='discard'){ canDiscard=new Set(a.tiles); }
    else if(a.action=='que'){ btns.push(...a.options.map(o=>
        `<button class="a" onclick="send('que','${o}')">定缺${SUIT[o]}</button>`)); }
    else if(a.action=='hu'){ btns.push(`<button class="a hu" onclick="send('hu')">胡！${a.note||''}</button>`); }
    else if(a.action=='peng'){ btns.push(`<button class="a" onclick="send('peng','${a.tiles[0]}')">碰 ${lbl(a.tiles[0])}</button>`); }
    else if(a.action=='gang'){ btns.push(...a.tiles.map(t=>
        `<button class="a" onclick="send('gang','${t}')">杠 ${lbl(t)}</button>`)); }
    else if(a.action=='pass'){ btns.push(`<button class="a gray" onclick="send('pass')">过</button>`); }
  }
  document.getElementById('acts').innerHTML=btns.join('');

  // 我的碰杠明牌区（手牌右侧、稍矮）
  document.getElementById('mymeld').innerHTML=((WATCH?(me&&me.melds):S.melds)||[]).map(m=>
     `<span class="g"><b>${MELD[m.type]}</b>${m.text.split(' ').map(x=>`<i>${x}</i>`).join(' ')}</span>`).join('');

  // 手牌（观战：谁的手牌都不给，只写一行「观战中」）
  if(WATCH){
    const q=me&&me.que?` · 缺${SUIT[me.que]}`:'';
    const tag=me?(me.type=='bot'?' 🤖':(me.type=='api'?' 🕯️':''))+(me.autoplay?' · 托管中':''):'';
    const hu=me&&me.hu?` · 🎉${(me.hu.flags||[]).join('/')} ${me.hu.fan}番`:'';
    document.getElementById('hand').innerHTML=`<div class="watchnote"><b>👁 观战中</b>底边：${me?['东','南','西','北'][me.seat]+'座':''} ${me?me.name:''}${tag}${q} · ${me?me.hand_count:0} 张${hu}</div>`;
  }else
  document.getElementById('hand').innerHTML=S.hand_text.map((h,i)=>{
    const on=canDiscard&&canDiscard.has(h.t);
    return `<button class="t ${on?'on':'off'}" data-t="${h.t}" ${on?`onclick="MJFeel.tap(this,${i},'${h.t}')"`:'disabled'}>
      <span class="e">${h.e}</span><small>${h.n}</small></button>`;
  }).join('');

  // 终局
  const ov=document.getElementById('over');
  if(S.phase=='over'){
    ov.style.display='flex';
    document.getElementById('overT').textContent='终局 · '+(S.over_reason||'');
    document.getElementById('overS').innerHTML=S.players.map(p=>{
      const v=S.scores?S.scores[p.seat]:0;
      const hu=p.hu?`（${(p.hu.flags||[]).join('/')} ${p.hu.fan}番）`:'';
      return `${WATCH?SEATN[p.seat]:DIRS[rel(p.seat)]} ${p.name} <b>${v>0?'+':''}${v}</b> ${hu}`;
    }).join('<br>');
  }else{ ov.style.display='none'; }
}
document.addEventListener('click',function unlock(){   // 首次交互解锁移动端音频
  try{ const a=new Audio('/mj/static/sfx/draw.wav'); a.volume=0; a.play().catch(()=>{}); }catch(e){}
  document.removeEventListener('click',unlock);
},{once:true});
pull(); setInterval(pull,2000);
</script></body></html>"""


@app.route("/mj/t/<tid>/<token>")
def web_seat(tid, token):
    t = load_table(tid)
    if not t or token not in t["tokens"]:
        return Response(GONE_PAGE, mimetype="text/html; charset=utf-8", status=404)
    seat = t["tokens"][token]
    if t["state"]:
        name = t["state"]["players"][seat]["name"]
    else:
        name = (t["room"]["seats"][seat] or {}).get("name", "")
    html = (PAGE.replace("__TABLE__", tid).replace("__TOKEN__", token).replace("__WATCH__", "")
                .replace("__NAME__", name))
    return Response(html, mimetype="text/html; charset=utf-8")


@app.route("/mj/w/<tid>/<wt>")
def web_watch(tid, wt):
    """观战页：复用牌桌页，TOKEN 为空、WATCH 为观战票；底部不出手牌、没有按钮。"""
    t, err = watch_auth(tid, wt)
    if err:
        return Response(GONE_PAGE, mimetype="text/html; charset=utf-8", status=404)
    html = (PAGE.replace("__TABLE__", tid).replace("__TOKEN__", "").replace("__WATCH__", wt)
                .replace("__NAME__", "观战 " + tid))
    return Response(html, mimetype="text/html; charset=utf-8")


GONE_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>座位不在了</title>
<style>body{margin:0;background:#12140f;color:#e8e6dd;font:16px/1.7 -apple-system,"PingFang SC",sans-serif;
padding:40px 24px;text-align:center}a{color:#e8c46a}</style></head><body>
<p>这个座位已经不在了（房间解散，或太久没响应被释放）。</p><p><a href="/mj/">回大厅重新组局</a></p></body></html>"""


INDEX = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>神庭麻将 · 大厅</title><style>
:root{--bg:#12140f;--card:#1a1e16;--line:#2e3327;--fg:#e8e6dd;--dim:#9da593;--gold:#e8c46a;--go:#6fae57}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,"PingFang SC",system-ui,sans-serif;
 padding:16px 16px calc(24px + env(safe-area-inset-bottom))}
h1{font-size:20px;margin:4px 0 2px}
.sub{color:var(--dim);font-size:13px;margin:0 0 14px}
label{display:block;color:var(--dim);font-size:12px;margin:0 0 4px}
input{background:#1c1f18;color:var(--fg);border:1px solid var(--line);border-radius:9px;padding:10px 12px;font-size:16px;width:100%}
.me{display:flex;gap:8px;align-items:center;margin-bottom:14px}
.me input{flex:1}
.me .av{width:40px;height:40px;border-radius:50%;background:#22271d;display:flex;align-items:center;justify-content:center;font-size:20px;flex:0 0 auto}
.btns{display:flex;gap:8px;margin-bottom:8px}
button{border:none;border-radius:10px;padding:12px 14px;font-size:15px;font-weight:700;color:#0e120c;background:var(--go)}
button.gold{background:var(--gold);color:#3a2c00}
button.ghost{background:#22271d;color:var(--fg);border:1px solid var(--line)}
button:disabled{opacity:.45}
.btns button{flex:1}
.code{display:flex;gap:8px;margin-bottom:18px}
.code input{flex:1;letter-spacing:2px;font-size:18px}
.code button{flex:0 0 auto}
h2{font-size:14px;color:var(--dim);margin:14px 0 8px;font-weight:600}
.room{padding:12px;border:1px solid var(--line);border-radius:12px;background:var(--card);margin-bottom:10px}
.room.hi{border-color:var(--gold)}
.room .t{display:flex;align-items:center;gap:8px}
.room .nm{font-weight:700;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.room .cd{color:var(--gold);font-weight:800;letter-spacing:1px}
.room .need{font-size:12px;color:var(--dim);white-space:nowrap}
.seats{display:grid;grid-template-columns:1fr 1fr;gap:4px 10px;margin:8px 0}
.seat{font-size:13px;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.seat b{color:var(--fg);font-weight:600}
.seat .ok{color:#9be07a}
.seat .dir{display:inline-block;width:16px;color:var(--dim);opacity:.7}
.room .act{display:flex;gap:8px;align-items:center}
.room .act button{width:100%;padding:9px}
.room .act button.w{width:auto;flex:0 0 auto;padding:9px 12px;font-size:13px}
.empty{color:var(--dim);font-size:14px;padding:18px 8px;text-align:center;border:1px dashed var(--line);border-radius:12px}
#msg{color:var(--gold);font-size:13px;min-height:18px;margin-bottom:6px}
.hint{color:var(--dim);font-size:12px;margin-top:18px;line-height:1.6}
</style></head><body>
<h1>🀄️ 神庭麻将</h1>
<p class="sub">四川血战到底 · 人和小机同桌 · 缺人的座位笨机顶上</p>

<div class="me"><div class="av">👤</div>
 <div style="flex:1"><label>你的名字（不填就叫路人）</label><input id="nick" maxlength="12" placeholder="比如：折月家长"></div>
</div>
<div id="msg"></div>
<div class="btns">
 <button class="gold" onclick="quick(this)">⚡ 快速加入</button>
 <button onclick="create(this)">＋ 建一房</button>
</div>
<div class="code"><input id="code" inputmode="numeric" maxlength="6" placeholder="输房号加入" value="__ROOM__"><button class="ghost" onclick="joinCode(this)">进</button></div>

<div id="pinned"></div>
<h2 id="hWait">正在组的局</h2>
<div id="waiting"><div class="empty">加载中…</div></div>
<h2 id="hPlay" style="display:none">打牌中</h2>
<div id="playing"></div>
<p class="hint">建房后会进候场：把房号或链接发给朋友，各自进来点「准备」；四座齐了自动开局，或房主点开局、空座由笨机补。<br>小机也能用自己的名字来坐，让它的家长把候场页里的「叫我的小机来」发给它就行。</p>
<script>
const PRE_ROOM="__ROOM__";
function ident(){
  let id=null; try{ id=localStorage.getItem('mj_ident'); }catch(e){}
  if(!id||!/^guest:[A-Za-z0-9_-]{6,40}$/.test(id)){
    id='guest:'+Array.from(crypto.getRandomValues(new Uint8Array(9)),b=>('0'+b.toString(16)).slice(-2)).join('');
    try{ localStorage.setItem('mj_ident',id); }catch(e){}
  }
  return id;
}
const nickEl=document.getElementById('nick');
try{ nickEl.value=localStorage.getItem('mj_nick')||''; }catch(e){}
nickEl.addEventListener('change',()=>{ try{ localStorage.setItem('mj_nick',nickEl.value.trim()); }catch(e){} });
function who(){ return {ident:ident(), nick:nickEl.value.trim()}; }
function say(t){ document.getElementById('msg').textContent=t||''; }
async function post(path,body,btn){
  if(btn){ btn.disabled=true; }
  say('');
  try{
    const r=await fetch('/mj/api/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(Object.assign(who(),body||{}))});
    const j=await r.json();
    if(!j.ok){ say('✗ '+j.error); return null; }
    return j;
  }catch(e){ say('网络卡了，再试一次'); return null; }
  finally{ if(btn) btn.disabled=false; }
}
function goSeat(j){ if(j&&j.web_url) location.href=j.web_url; }
async function quick(btn){ goSeat(await post('queue',{},btn)); }
async function create(btn){ goSeat(await post('rooms',{visible:true},btn)); }
async function joinCode(btn){
  const code=document.getElementById('code').value.trim();
  if(!/^\\d{6}$/.test(code)){ say('房号是 6 位数字'); return; }
  goSeat(await post('join',{room:code},btn));
}
async function joinRoom(code,btn){ goSeat(await post('join',{room:code},btn)); }
// 观战：领一张观战票（不占座），进只读牌桌 / 复制链接
async function watchRoom(code,btn){ const j=await post('watch',{room:code},btn); if(j&&j.watch_url) location.href=j.watch_url; }
async function copyWatch(code,btn){
  const j=await post('watch',{room:code},btn); if(!j||!j.watch_url) return;
  const url=location.origin+j.watch_url, old=btn.textContent;
  try{ await navigator.clipboard.writeText(url); }catch(e){ window.prompt('复制这段：',url); }
  btn.textContent='已复制 ✓'; setTimeout(()=>btn.textContent=old,1500);
}

function seatLine(s){
  if(s.state==='empty') return `<div class="seat"><span class="dir">${s.dir}</span>◯ 空</div>`;
  if(s.state==='reserved') return `<div class="seat"><span class="dir">${s.dir}</span>⏳ 留给 ${s.for_name}</div>`;
  const av=s.kind==='machine'?'🕯️':(s.kind==='bot'?'🤖':'👤');
  const ok=s.state==='ready'?' <span class="ok">✓</span>':'';
  return `<div class="seat"><span class="dir">${s.dir}</span>${av} <b>${s.name}</b>${s.is_host?' 👑':''}${ok}</div>`;
}
function card(r,hi){
  const seats=r.seats.map(seatLine).join('');
  if(r.phase==='playing'){
    return `<div class="room"><div class="t"><span class="nm">${r.name}</span><span class="cd">${r.room}</span><span class="need">打牌中 · 墙 ${r.wall}</span></div><div class="seats">${seats}</div>
    <div class="act"><button class="ghost" onclick="watchRoom('${r.room}',this)">👁 围观${r.watching?' ('+r.watching+')':''}</button><button class="ghost w" onclick="copyWatch('${r.room}',this)">复制围观链接</button></div></div>`;
  }
  const can=r.need>0;
  return `<div class="room ${hi?'hi':''}"><div class="t"><span class="nm">${r.name}</span><span class="cd">${r.room}</span><span class="need">${can?'差 '+r.need+' 人':'满员待开'}</span></div>
    <div class="seats">${seats}</div>
    <div class="act"><button ${can?'':'disabled'} onclick="joinRoom('${r.room}',this)">${can?'加入这局':'满了'}</button><button class="ghost w" onclick="watchRoom('${r.room}',this)">👁 围观</button><button class="ghost w" onclick="copyWatch('${r.room}',this)">围观链接</button></div></div>`;
}
async function refresh(){
  try{
    const r=await fetch('/mj/api/rooms'); const j=await r.json(); if(!j.ok) return;
    const w=j.rooms.filter(r=>r.phase==='waiting'), p=j.rooms.filter(r=>r.phase==='playing');
    document.getElementById('hWait').textContent=`正在组的局 (${w.length})`;
    document.getElementById('waiting').innerHTML = w.length ? w.map(r=>card(r,false)).join('')
      : '<div class="empty">还没人开局。点「快速加入」会替你开第一桌。</div>';
    document.getElementById('hPlay').style.display = p.length?'':'none';
    document.getElementById('hPlay').textContent=`打牌中 (${p.length})`;
    document.getElementById('playing').innerHTML = p.map(r=>card(r,false)).join('');
    if(PRE_ROOM){
      const pr=await (await fetch('/mj/api/room?room='+PRE_ROOM)).json();
      document.getElementById('pinned').innerHTML = pr.ok
        ? `<h2>朋友邀你进这局</h2>`+card(pr.room,true)
        : `<div class="empty">房 ${PRE_ROOM} 已经不在了（解散或打完了）</div>`;
    }
  }catch(e){}
}
if(location.search.indexOf('gone=1')>=0) say('那一桌已经散了，重新来一局吧');
refresh(); setInterval(refresh,3000);
</script></body></html>"""


@app.route("/mj/")
@app.route("/mj")
def index():
    return Response(INDEX.replace("__ROOM__", ""), mimetype="text/html; charset=utf-8")


@app.route("/mj/r/<code>")
def room_invite(code):
    """房间邀请链接：大厅页 + 该房置顶，填名字点「加入这局」就入座。"""
    code = "".join(ch for ch in code if ch.isalnum())[:8]
    return Response(INDEX.replace("__ROOM__", code), mimetype="text/html; charset=utf-8")


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8791))
    print(f"川麻服务 http://127.0.0.1:{port}/mj/")
    app.run(host="0.0.0.0", port=port, threaded=True)
