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
import json
import os
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


def create_key_configured():
    return bool(os.environ.get("MJ_CREATE_KEY"))


def check_create_permission(req):
    """Production can require MJ_CREATE_KEY; local dev remains frictionless."""
    key = os.environ.get("MJ_CREATE_KEY")
    if key:
        supplied = req.headers.get("X-MJ-Create-Key") or (req.get_json(silent=True) or {}).get("create_key", "")
        if not supplied or not hmac.compare_digest(str(supplied), key):
            return False, "建桌需要有效的创建密钥"
    ip = req.headers.get("X-Forwarded-For", req.remote_addr or "-").split(",")[0].strip()
    now_ts = time.time()
    attempts = [t for t in CREATE_ATTEMPTS.get(ip, []) if now_ts - t < CREATE_WINDOW_SEC]
    if len(attempts) >= CREATE_LIMIT:
        CREATE_ATTEMPTS[ip] = attempts
        return False, "建桌太频繁，请稍后再试"
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


def load_table(tid):
    with db() as conn:
        r = conn.execute("SELECT * FROM tables WHERE id=?", (tid,)).fetchone()
    if not r:
        return None
    return {"id": r["id"], "tokens": json.loads(r["tokens"]), "state": json.loads(r["state"])}


def save_table(tid, state):
    with db() as conn:
        conn.execute("UPDATE tables SET state=?, updated_at=? WHERE id=?",
                     (json.dumps(state, ensure_ascii=False), now(), tid))


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
            return jsonify({"ok": False, "error": err[0]}), err[1]
        g = t["state"]
        apply_timeouts(g)
        touch_player(g, s, reconnected=True)
        E.run_bots(g, limit=5000)
        save_table(tid, g)
        v = E.view(g, s)
    v.update({"ok": True, "table": tid})
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
    v.update({"ok": True, "table": tid})
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
.backs{display:flex;gap:2px;flex-wrap:wrap;justify-content:center}
.backs.vert{flex-direction:column}
.back{background:linear-gradient(135deg,#2c7a52,#12492e);border:1px solid #0b3620;
 border-radius:3px;box-shadow:inset 0 1px 0 rgba(255,255,255,.18)}
.backs .back{width:13px;height:19px}
.backs.vert .back{width:22px;height:13px}
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
</style></head><body>
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
    if(!j.ok){ document.getElementById('msg').textContent=j.error; return; }
    apply(j);
  }catch(e){}
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
function apply(j){ const old=S; S=j; try{ sfxDiff(old,j); }catch(e){} render(); }

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
        abort(404)
    seat = t["tokens"][token]
    name = t["state"]["players"][seat]["name"]
    html = (PAGE.replace("__TABLE__", tid).replace("__TOKEN__", token)
                .replace("__NAME__", name))
    return Response(html, mimetype="text/html; charset=utf-8")


INDEX = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>川麻开桌</title><style>
body{margin:0;background:#12140f;color:#e8e6dd;font:16px/1.7 -apple-system,"PingFang SC",sans-serif;padding:18px}
h1{font-size:19px}
label{display:block;margin:10px 0 3px;color:#8d9182;font-size:13px}
input,select{background:#1c1f18;color:#e8e6dd;border:1px solid #2e3327;border-radius:7px;
 padding:9px;font-size:16px;width:100%}
.row{display:flex;gap:8px;align-items:flex-end}.row>div{flex:1}
button{background:#6fae57;color:#0e120c;border:none;border-radius:8px;padding:12px 20px;
 font-size:16px;font-weight:700;margin-top:16px;width:100%}
#out{margin-top:18px;font-size:14px;line-height:1.6}
a{color:#d9b25f}
.room{margin-top:14px;padding:12px;border:1px solid #39412e;border-radius:12px;background:#1a1e16}
.room-title{display:flex;justify-content:space-between;align-items:center;gap:8px}
.room-code{font-size:22px;letter-spacing:2px;color:#e8c46a;font-weight:800}
.seat-card{display:flex;align-items:center;gap:8px;margin-top:8px;padding:9px;border-radius:9px;background:#22271d}
.seat-card .seat-info{flex:1;min-width:0}.seat-card small{display:block;color:#9da593;overflow-wrap:anywhere}
.seat-card button,.room-title button{width:auto;margin:0;padding:7px 10px;font-size:13px;background:#526f45;color:#f1f3e8}
.seat-card .bot{color:#9da593;font-size:13px;white-space:nowrap}
.hint{color:#9da593;font-size:13px;margin-top:9px}
</style></head><body>
<h1>🀄️ 开一桌川麻</h1>
<div id="f"></div>
<button onclick="go()">开桌</button>
<div id="out"></div>
<script>
const seats=["东","南","西","北"];
document.getElementById('f').innerHTML=seats.map((s,i)=>`
<div class="row"><div><label>${s}家${i==0?'（庄）':''} 名字</label>
<input id="n${i}" value="${i==0?'游':'笨机'+i}"></div>
<div><label>类型</label><select id="t${i}">
<option value="${i==0?'human':'bot'}">${i==0?'人类（网页）':'笨 bot'}</option>
<option value="${i==0?'bot':'human'}">${i==0?'笨 bot':'人类（网页）'}</option>
<option value="api">小机（API）</option></select></div></div>`).join('');
async function go(){
  const seats=[0,1,2,3].map(i=>({name:document.getElementById('n'+i).value,
                                 type:document.getElementById('t'+i).value}));
  const r=await fetch('/mj/api/new_table',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({seats})});
  const j=await r.json();
  if(!j.ok){document.getElementById('out').textContent=j.error;return;}
  const origin=location.origin;
  const absolute=s=>new URL(s.web_url,origin).href;
  const copy=async(text,button)=>{
    try{ await navigator.clipboard.writeText(text); button.textContent='已复制 ✓'; }
    catch(e){ window.prompt('复制这段内容：',text); }
    setTimeout(()=>button.textContent='复制邀请',1600);
  };
  const share=async(text,button)=>{
    if(navigator.share){ try{ await navigator.share({title:'神庭麻将',text}); button.textContent='已分享 ✓'; setTimeout(()=>button.textContent='分享',1600); return; }catch(e){} }
    copy(text,button);
  };
  const cards=j.seats.map(s=>{
    if(s.type=='bot') return `<div class="seat-card"><div class="seat-info"><b>${s.seat_name}家 · ${s.name}</b><small>笨 bot 自动补位</small></div><span class="bot">🤖</span></div>`;
    const text=s.type=='api'
      ? `神庭麻将小机座位\n桌号：${j.table}\n座位：${s.seat}\nToken：${s.token}\n状态接口：${origin}/mj/api/state?table=${j.table}&seat=${s.seat}&token=${s.token}`
      : `来神庭麻将和我玩：${absolute(s)}`;
    return `<div class="seat-card"><div class="seat-info"><b>${s.seat_name}家 · ${s.name}</b><small>${s.type=='api'?'小机 API 座位':'人类网页座位'} · ${s.type=='api'?'请把接入信息发给小机':'打开链接即可入座'}</small></div>`+
      `<button onclick='copy(${JSON.stringify(text)},this)'>复制邀请</button>`+
      (s.type=='human'?`<button onclick='share(${JSON.stringify(text)},this)'>分享</button>`:'')+
      `</div>`;
  }).join('');
  document.getElementById('out').innerHTML=`<div class="room"><div class="room-title"><div>房间码 <span class="room-code">${j.table}</span></div><button onclick='copy(${JSON.stringify(j.table)},this)'>复制房间码</button></div>${cards}<div class="hint">把对应座位的邀请链接发给家长；笨 bot 会自动补空位。</div></div>`;
}
</script></body></html>"""


@app.route("/mj/")
@app.route("/mj")
def index():
    return Response(INDEX, mimetype="text/html; charset=utf-8")


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8791))
    print(f"川麻服务 http://127.0.0.1:{port}/mj/")
    app.run(host="0.0.0.0", port=port, threaded=True)
