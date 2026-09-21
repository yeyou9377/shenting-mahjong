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
           "created_at": r["created_at"], "log": r.get("log", [])[-8:]}
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
    tid = request.args.get("table", "")
    token = request.args.get("token", "")
    seat = request.args.get("seat")
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
    v.update({"ok": True, "table": tid})
    if t["room"]:
        v["room"] = room_public(t, s)
    return jsonify(v)


@app.route("/mj/api/act", methods=["POST"])
def api_act():
    body = request.get_json(silent=True) or {}
    tid = body.get("table", "")
    token = body.get("token", "")
    seat = body.get("seat")
    action = (body.get("action") or "").strip()
    tile = body.get("tile")
    with LOCK:
        t, s, err = auth(tid, seat, token)
        if err:
            return jsonify({"ok": False, "error": err[0]}), err[1]
        g = t["state"]
        apply_timeouts(g)
        touch_player(g, s, reconnected=True)
        try:
            E.act(g, s, action, tile)
            E.run_bots(g, limit=5000)
        except E.MJError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "error": f"引擎出错：{e}"}), 500
        save_table(tid, g)
        v = E.view(g, s)
        for p in v["players"]:
            p["partner_of"] = g["players"][p["seat"]].get("partner_of")
    v.update({"ok": True, "table": tid})
    return jsonify(v)


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
#seatTop{top:6px;left:50%;transform:translateX(-50%);max-width:70%}
#seatLeft{left:4px;top:46%;transform:translateY(-50%);align-items:flex-start;max-width:24%}
#seatRight{right:4px;top:46%;transform:translateY(-50%);align-items:flex-end;max-width:24%}

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
.backs .back{width:18px;height:27px}
.backs.vert .back{width:27px;height:18px}
.omeld{display:flex;gap:3px;flex-wrap:wrap;justify-content:center;font-size:15px;margin-top:1px}
.omeld .g{background:rgba(232,196,106,.16);border:1px solid rgba(232,196,106,.4);
 border-radius:4px;padding:0 3px;line-height:1.5}
.ohu{color:var(--gold);font-weight:700;font-size:12px}

/* ---- 弃牌河 ---- */
#river{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
 width:52%;height:52%;display:grid;z-index:1;
 grid-template:"tp tp tp" auto "lf md rt" 1fr "bt bt bt" auto/auto 1fr auto;
 gap:2px}
.zone{display:flex;flex-wrap:wrap;gap:2px;align-content:flex-start;overflow:hidden}
#zTop{grid-area:tp;justify-content:center}
#zBot{grid-area:bt;justify-content:center}
#zLeft{grid-area:lf;flex-direction:column;width:26px}
#zRight{grid-area:rt;flex-direction:column;width:26px}
#zMid{grid-area:md;display:flex;align-items:center;justify-content:center;text-align:center;
 font-size:12px;color:var(--dim);padding:2px}
.rt{font-size:19px;line-height:1;filter:drop-shadow(0 1px 1px rgba(0,0,0,.5))}
.rt.hot{background:rgba(232,196,106,.9);border-radius:4px;outline:2px solid #fff3d0}
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
 min-width:46px;height:60px;padding:4px 3px;display:flex;flex-direction:column;
 align-items:center;justify-content:center;
 box-shadow:0 3px 0 #b7ad92,0 4px 7px rgba(0,0,0,.4)}
.t .e{font-size:30px;line-height:1}
.t small{font-size:10px;color:#5a5a5a;margin-top:1px}
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
   <button id="mute" onclick="toggleMute()">🔊</button>
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
const TABLE="__TABLE__", TOKEN="__TOKEN__";
let S=null, busy=false;
const MELD={peng:"碰",gang_ming:"明杠",gang_an:"暗杠",gang_bu:"补杠"};
const SUIT={w:"万",o:"筒",t:"条"};
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
    const a=cache[n]; if(!a) return;
    try{ a.currentTime=0; const p=a.play(); if(p&&p.catch) p.catch(()=>{}); }catch(e){}
  }
  return {play,
    set:(m)=>{muted=m; try{localStorage.setItem('mj_mute',m?'1':'0');}catch(e){}},
    get:()=>muted};
})();
function toggleMute(){ SFX.set(!SFX.get()); document.getElementById('mute').textContent=SFX.get()?'🔇':'🔊'; }

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
  if(busy) return;
  try{
    const r=await fetch(`/mj/api/state?table=${TABLE}&token=${TOKEN}`);
    const j=await r.json();
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
  if(!confirm('离开这一桌？')) return;
  await roomPost('leave'); location.href='/mj/';
}
function seatHTML(s,pos,r){
  const isMe=s.seat===S.my_seat;
  const cls=['ws','wpos'+pos, isMe?'me':'', s.state==='empty'?'empty':'', s.state==='ready'?'ready':''].join(' ');
  if(s.state==='empty'){
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
async function send(action,tile){
  if(busy) return; busy=true;
  document.getElementById('msg').textContent="…";
  try{
    const r=await fetch('/mj/api/act',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({table:TABLE,token:TOKEN,action,tile})});
    const j=await r.json();
    if(!j.ok){ document.getElementById('msg').textContent="✗ "+j.error; }
    else { document.getElementById('msg').textContent=""; apply(j); }
  }catch(e){ document.getElementById('msg').textContent="网络卡了，再点一次"; }
  busy=false;
}
function apply(j){
  if(j.phase==='waiting'){ S=j; renderWait(); return; }
  const wasWaiting = WAIT.classList.contains('on');
  WAIT.classList.remove('on');
  const old=(S&&S.phase==='waiting')?null:S; S=j;
  try{ if(wasWaiting) SFX.play('turn'); else sfxDiff(old,j); }catch(e){}
  render();
}

function rel(seat){ return ((seat - S.my_seat)%4+4)%4; }   // 0我 1右 2上 3左

function oppHTML(p){
  const turn=p.is_turn?'turn':'';
  const hu=p.hu?`<div class="ohu">🎉${(p.hu.flags||[]).join('/')} ${p.hu.fan}番</div>`:'';
  const backs=Array(p.hand_count).fill('<div class="back"></div>').join('');
  const vert=(rel(p.seat)!==2)?'vert':'';
  const mel=p.melds.map(m=>`<span class="g">${m.tiles.map(emo).join('')}</span>`).join('');
  return `<div class="pill ${turn}"><span class="dir">${DIRS[rel(p.seat)]}</span>${p.name}`+
    `${p.type=='bot'?' 🤖':(p.type=='api'?' 🕯️':'')} <span class="cnt">${p.hand_count}</span></div>`+
    `${mel?`<div class="omeld">${mel}</div>`:''}`+
    `<div class="backs ${vert}">${backs}</div>${hu}`;
}
function riverHTML(p){
  const hot=(t,i)=> (p.seat===S.last_discard_seat && i===p.discards.length-1 && S.last_discard)?'hot':'';
  return p.discards.map((t,i)=>`<span class="rt ${hot(t,i)}">${emo(t)}</span>`).join('');
}

function render(){
  const me=S.players.find(p=>p.is_me);
  document.getElementById('cWho').textContent='🀄️ '+S.my_name+(S.my_que?`·缺${SUIT[S.my_que]}`:'');
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
  document.getElementById('zMid').innerHTML = S.claim_tile
    ? `<span class="claim">抢 ${lbl(S.claim_tile)}</span>`
    : (S.phase=='play'?`轮到<br>${turnName}`:'');

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
  document.getElementById('mymeld').innerHTML=(S.melds||[]).map(m=>
     `<span class="g"><b>${MELD[m.type]}</b>${m.text}</span>`).join('');

  // 手牌
  document.getElementById('hand').innerHTML=S.hand_text.map(h=>{
    const on=canDiscard&&canDiscard.has(h.t);
    return `<button class="t ${on?'on':'off'}" ${on?`onclick="send('discard','${h.t}')"`:'disabled'}>
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
      return `${DIRS[rel(p.seat)]} ${p.name} <b>${v>0?'+':''}${v}</b> ${hu}`;
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
    html = (PAGE.replace("__TABLE__", tid).replace("__TOKEN__", token)
                .replace("__NAME__", name))
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
    return `<div class="room"><div class="t"><span class="nm">${r.name}</span><span class="cd">${r.room}</span><span class="need">打牌中 · 墙 ${r.wall}</span></div><div class="seats">${seats}</div></div>`;
  }
  const can=r.need>0;
  return `<div class="room ${hi?'hi':''}"><div class="t"><span class="nm">${r.name}</span><span class="cd">${r.room}</span><span class="need">${can?'差 '+r.need+' 人':'满员待开'}</span></div>
    <div class="seats">${seats}</div>
    <div class="act"><button ${can?'':'disabled'} onclick="joinRoom('${r.room}',this)">${can?'加入这局':'满了'}</button></div></div>`;
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
