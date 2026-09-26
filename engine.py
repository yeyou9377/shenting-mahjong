#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""川麻（血战到底）引擎 v0 —— 四人版。

从 ~/.dengxia-memory/room/games/chuanma.py 的二人命令行版扩展而来。
核心 can_hu（4面子+1对 的 DFS + 七对）算法原样保留，只做了三处修正：
  1) 支持已亮出的面子（melds），手牌张数按 14-3n 判断
  2) 缺门检查把 melds 里的牌也算进去
  3) 七对要求没有任何 meld

牌面记法：'5w' 五万 / '3t' 三条 / '9o' 九筒
状态是纯 dict，可直接 json 序列化存 sqlite。
"""
import random
from collections import Counter

SUITS = {"w": "万", "o": "筒", "t": "条"}
SUIT_ORDER = {"w": 0, "t": 1, "o": 2}
EMOJI = {
    "w": {1: "🀇", 2: "🀈", 3: "🀉", 4: "🀊", 5: "🀋", 6: "🀌", 7: "🀍", 8: "🀎", 9: "🀏"},
    "o": {1: "🀙", 2: "🀚", 3: "🀛", 4: "🀜", 5: "🀝", 6: "🀞", 7: "🀟", 8: "🀠", 9: "🀡"},
    "t": {1: "🀐", 2: "🀑", 3: "🀒", 4: "🀓", 5: "🀔", 6: "🀕", 7: "🀖", 8: "🀗", 9: "🀘"},
}
SEAT_NAMES = ["东", "南", "西", "北"]


# ---------- 牌面工具 ----------

def tile_str(t):
    n, s = int(t[0]), t[1]
    return f"{EMOJI[s][n]}{n}{SUITS[s]}"


def tile_emoji(t):
    return EMOJI[t[1]][int(t[0])]


def sort_tiles(tiles):
    return sorted(tiles, key=lambda x: (SUIT_ORDER[x[1]], int(x[0])))


def full_deck():
    return [f"{n}{s}" for s in "wot" for n in range(1, 10)] * 4


def meld_tiles(m):
    """一个面子占用的全部牌。"""
    if m["type"] == "peng":
        return [m["tile"]] * 3
    return [m["tile"]] * 4


# ---------- 胡牌判定（移植自 chuanma.py，算法未重写） ----------

def _decompose(cnt):
    """剩余牌能否全部拆成刻子/顺子。cnt 是 Counter。"""
    tiles_left = sorted([t for t, v in cnt.items() for _ in range(v)])
    if not tiles_left:
        return True
    t = tiles_left[0]
    n, s = int(t[0]), t[1]
    if cnt[t] >= 3:
        c2 = cnt.copy()
        c2[t] -= 3
        c2 += Counter()
        if _decompose(c2):
            return True
    t2, t3 = f"{n + 1}{s}", f"{n + 2}{s}"
    if n <= 7 and cnt[t2] > 0 and cnt[t3] > 0:
        c2 = cnt.copy()
        c2[t] -= 1
        c2[t2] -= 1
        c2[t3] -= 1
        c2 += Counter()
        if _decompose(c2):
            return True
    return False


def can_hu(hand, melds=None, que=None):
    """标准 4面子+1对；缺门必须打干净；七对也算（川麻）。

    hand  : 手上暗牌（含刚摸/刚吃的那张）
    melds : 已亮出的面子列表
    que   : 'w'/'o'/'t' 或 None
    返回 (bool, 牌型名)
    """
    melds = melds or []
    if que:
        if any(t[1] == que for t in hand):
            return False, "缺门没打干净"
        if any(m["tile"][1] == que for m in melds):
            return False, "缺门没打干净"

    n_meld = len(melds)
    if len(hand) != 14 - 3 * n_meld:
        return False, "张数不对"

    c = Counter(hand)
    # 七对：必须门清（没有亮出的面子）
    if n_meld == 0 and len(hand) == 14 and all(v in (2, 4) for v in c.values()) \
            and sum(v // 2 for v in c.values()) == 7:
        return True, "七对"

    for pair in [t for t, v in c.items() if v >= 2]:
        c2 = c.copy()
        c2[pair] -= 2
        c2 += Counter()
        if _decompose(c2):
            return True, "平胡"
    return False, "牌型不成"


def hu_score(hand, melds, que, *, gang_count=0, self_draw=False):
    """按神庭麻将馆规计分，返回 (ok, {'type', 'fan', 'flags'}).

    馆规（倍率相乘）：平胡×1、清一色×2、七对×2、每根杠×2，
    自摸×2，不设封顶。胡牌判定仍完全委托给 ``can_hu``。
    """
    ok, typ = can_hu(hand, melds, que)
    if not ok:
        return False, {"type": typ, "fan": 0, "flags": []}
    fan = 2 if typ == "七对" else 1
    flags = [typ]
    allt = list(hand) + [t for m in melds for t in meld_tiles(m)]
    if len(set(t[1] for t in allt)) == 1:
        fan *= 2
        flags.append("清一色")
    for _ in range(max(0, int(gang_count))):
        fan *= 2
        flags.append("杠")
    if self_draw:
        fan *= 2
        flags.append("自摸")
    return True, {"type": typ, "fan": fan, "flags": flags}


# ---------- 建局 ----------

def new_game(seats, seed=None):
    """seats: [{'name':..,'type':'human'|'bot'|'api'}, x4]"""
    rng = random.Random(seed)
    deck = full_deck()
    rng.shuffle(deck)
    players = []
    for i, s in enumerate(seats):
        n = 14 if i == 0 else 13
        hand = sort_tiles([deck.pop(0) for _ in range(n)])
        players.append({
            "seat": i,
            "name": s.get("name") or f"{SEAT_NAMES[i]}家",
            "type": s.get("type", "bot"),
            "autoplay": False,
            "last_seen": None,
            "disconnected_at": None,
            "hand": hand,
            "melds": [],
            "discards": [],
            "que": None,
            "hu": None,          # {'type','fan','flags','self_draw','from_seat'}
            "gang_count": 0,
        })
    return {
        "deck": deck,
        "players": players,
        "dealer": 0,
        "turn": 0,
        "phase": "que",           # que / play / over
        "step": "wait_que",       # wait_que / discard / claim
        "claim": None,
        "last_discard": None,
        "last_discard_seat": None,
        "log": ["开局，庄家是 " + players[0]["name"]],
        "scores": None,
        "over_reason": None,
        "hu_seq": 0,
    }


# ---------- 查询 ----------

def alive_seats(g):
    return [p["seat"] for p in g["players"] if not p["hu"]]


def next_alive(g, seat):
    for k in range(1, 5):
        s = (seat + k) % 4
        if not g["players"][s]["hu"]:
            return s
    return None


def _is_que(p, tile):
    """血战到底：本家缺门的牌不能碰、不能杠（碰了就永远胡不了）。9/26 游实战踩到。"""
    return p.get("que") is not None and tile[1] == p["que"]


def can_peng(p, tile):
    return (not _is_que(p, tile)) and p["hand"].count(tile) >= 2


def can_ming_gang(p, tile):
    return (not _is_que(p, tile)) and p["hand"].count(tile) >= 3


def self_gang_options(p):
    """轮到自己时可以杠的牌：暗杠(手上4张) + 补杠(已碰过且手上有第4张)。"""
    out = []
    for t, v in Counter(p["hand"]).items():
        if v == 4 and not _is_que(p, t):
            out.append({"tile": t, "kind": "an"})
    for m in p["melds"]:
        if m["type"] == "peng" and m["tile"] in p["hand"]:
            out.append({"tile": m["tile"], "kind": "bu"})
    return out


def available_actions(g, seat):
    """该座位此刻能做什么。"""
    p = g["players"][seat]
    if g["phase"] == "over":
        return []
    if p["hu"]:
        return []
    acts = []
    if g["phase"] == "que":
        if p["que"] is None:
            acts.append({"action": "que", "options": ["w", "o", "t"]})
        return acts
    if g["step"] == "claim":
        cl = g["claim"]
        mine = cl["eligible"].get(str(seat))
        if mine and str(seat) not in cl["responses"]:
            for a in mine:
                if a == "gang":
                    acts.append({"action": "gang", "tiles": [cl["tile"]]})
                elif a == "peng":
                    acts.append({"action": "peng", "tiles": [cl["tile"]]})
                else:
                    acts.append({"action": a})
            acts.append({"action": "pass"})
        return acts
    if g["step"] == "discard" and g["turn"] == seat:
        ok, info = hu_score(p["hand"], p["melds"], p["que"],
                            gang_count=p["gang_count"], self_draw=True)
        if ok:
            acts.append({"action": "hu", "note": "/".join(info["flags"])})
        gs = self_gang_options(p)
        if gs and g["deck"]:
            acts.append({"action": "gang", "tiles": sorted(set(x["tile"] for x in gs))})
        acts.append({"action": "discard", "tiles": sort_tiles(set(p["hand"]))})
    return acts


# ---------- 动作 ----------

class MJError(Exception):
    pass


def act(g, seat, action, tile=None):
    p = g["players"][seat]
    if g["phase"] == "over":
        raise MJError("这局已经结束了")
    if p["hu"] and action != "que":
        raise MJError("你已经胡了，这局不用再出手")

    if action == "que":
        if g["phase"] != "que":
            raise MJError("已经过了定缺阶段")
        if tile not in ("w", "o", "t"):
            raise MJError("缺门只能是 w/o/t")
        p["que"] = tile
        g["log"].append(f"{p['name']} 定缺{SUITS[tile]}")
        if all(x["que"] for x in g["players"]):
            g["phase"] = "play"
            g["step"] = "discard"
            g["turn"] = g["dealer"]
            g["log"].append("定缺完毕，开打")
        return g

    if g["phase"] != "play":
        raise MJError("还在定缺，先定缺")

    if g["step"] == "claim":
        return _claim_act(g, seat, action, tile)

    if g["turn"] != seat:
        raise MJError("还没轮到你")

    if action == "discard":
        if tile not in p["hand"]:
            raise MJError(f"手里没有 {tile}")
        p["hand"].remove(tile)
        p["discards"].append(tile)
        g["last_discard"] = tile
        g["last_discard_seat"] = seat
        g["log"].append(f"{p['name']} 打 {tile_str(tile)}")
        _open_claim(g, seat, tile)
        return g

    if action == "hu":
        ok, info = hu_score(p["hand"], p["melds"], p["que"],
                            gang_count=p["gang_count"], self_draw=True)
        if not ok:
            raise MJError("胡不了：" + info["type"])
        g["hu_seq"] += 1
        info.update({"self_draw": True, "from_seat": None, "order": g["hu_seq"]})
        p["hu"] = info
        g["log"].append(f"🎉 {p['name']} 自摸！{'/'.join(info['flags'])} {info['fan']}番")
        if not _after_hu(g, seat):
            # 血战到底：胡家退出轮转，下家接着摸
            _advance_after_discard(g, seat)
        return g

    if action == "gang":
        opts = {x["tile"]: x["kind"] for x in self_gang_options(p)}
        if tile not in opts:
            raise MJError("这张杠不了")
        if not g["deck"]:
            raise MJError("牌墙空了，不能杠")
        kind = opts[tile]
        if kind == "an":
            for _ in range(4):
                p["hand"].remove(tile)
            p["melds"].append({"type": "gang_an", "tile": tile, "from": seat})
            g["log"].append(f"{p['name']} 暗杠 {tile_str(tile)}")
        else:
            p["hand"].remove(tile)
            for m in p["melds"]:
                if m["type"] == "peng" and m["tile"] == tile:
                    m["type"] = "gang_bu"
                    break
            g["log"].append(f"{p['name']} 补杠 {tile_str(tile)}")
        p["gang_count"] += 1
        _draw_to(g, seat)
        return g

    raise MJError(f"不认识的动作 {action}")


def _open_claim(g, from_seat, tile):
    """开一个抢牌窗口；没人能抢就直接过。"""
    eligible = {}
    for p in g["players"]:
        if p["hu"] or p["seat"] == from_seat:
            continue
        opts = []
        hand2 = p["hand"] + [tile]
        ok, _ = can_hu(hand2, p["melds"], p["que"])
        if ok:
            opts.append("hu")
        if can_ming_gang(p, tile) and g["deck"]:
            opts.append("gang")
        if can_peng(p, tile):
            opts.append("peng")
        if opts:
            eligible[str(p["seat"])] = opts
    if not eligible:
        _advance_after_discard(g, from_seat)
        return
    g["step"] = "claim"
    g["claim"] = {"tile": tile, "from": from_seat, "eligible": eligible, "responses": {}}


def _claim_act(g, seat, action, tile=None):
    cl = g["claim"]
    key = str(seat)
    if key not in cl["eligible"]:
        raise MJError("你没有可以抢的牌")
    if key in cl["responses"]:
        raise MJError("你已经表过态了")
    if action == "pass":
        cl["responses"][key] = "pass"
    elif action in cl["eligible"][key]:
        cl["responses"][key] = action
    else:
        raise MJError(f"你不能 {action}")
    if len(cl["responses"]) == len(cl["eligible"]):
        _resolve_claim(g)
    return g


def _resolve_claim(g):
    cl = g["claim"]
    tile = cl["tile"]
    src = cl["from"]
    hus = [int(k) for k, v in cl["responses"].items() if v == "hu"]
    g["step"] = "discard"
    g["claim"] = None

    if hus:
        # 一炮多响：都算胡（血战到底不散桌）
        for s in sorted(hus):
            p = g["players"][s]
            ok, info = hu_score(p["hand"] + [tile], p["melds"], p["que"],
                                gang_count=p["gang_count"], self_draw=False)
            if not ok:
                continue
            g["hu_seq"] += 1
            info.update({"self_draw": False, "from_seat": src, "order": g["hu_seq"]})
            p["hu"] = info
            g["log"].append(
                f"🎉 {p['name']} 胡 {tile_str(tile)}（点炮：{g['players'][src]['name']}）"
                f" {'/'.join(info['flags'])} {info['fan']}番")
        g["players"][src]["discards"].remove(tile)
        g["last_discard"] = None
        for s in sorted(hus):
            if _after_hu(g, s):
                return
        if g["phase"] == "over":
            return
        nxt = next_alive(g, src)
        if nxt is None:
            return _end(g, "没有活家了")
        _draw_to(g, nxt)
        return

    gangs = [int(k) for k, v in cl["responses"].items() if v == "gang"]
    if gangs:
        s = gangs[0]
        p = g["players"][s]
        for _ in range(3):
            p["hand"].remove(tile)
        p["melds"].append({"type": "gang_ming", "tile": tile, "from": src})
        p["gang_count"] += 1
        g["players"][src]["discards"].remove(tile)
        g["last_discard"] = None
        g["log"].append(f"{p['name']} 明杠 {tile_str(tile)}")
        _draw_to(g, s)
        return

    pengs = [int(k) for k, v in cl["responses"].items() if v == "peng"]
    if pengs:
        s = pengs[0]
        p = g["players"][s]
        p["hand"].remove(tile)
        p["hand"].remove(tile)
        p["melds"].append({"type": "peng", "tile": tile, "from": src})
        g["players"][src]["discards"].remove(tile)
        g["last_discard"] = None
        g["log"].append(f"{p['name']} 碰 {tile_str(tile)}")
        g["turn"] = s
        g["step"] = "discard"
        return

    _advance_after_discard(g, src)


def _advance_after_discard(g, from_seat):
    nxt = next_alive(g, from_seat)
    if nxt is None:
        return _end(g, "没有活家了")
    _draw_to(g, nxt)


def _draw_to(g, seat):
    """给 seat 摸一张，进入出牌状态。墙尽则终局。

    摸到最后一张后牌墙即空：若该玩家能自摸胡，留给他决定（保留 discard 状态和 hu 动作）；
    否则不再要求他 discard，直接流局结算——这才是牌墙摸空的正确终局点。
    """
    if not g["deck"]:
        return _end(g, "牌墙摸完，流局")
    t = g["deck"].pop(0)
    p = g["players"][seat]
    p["hand"] = sort_tiles(p["hand"] + [t])
    g["turn"] = seat
    g["step"] = "discard"
    g["log"].append(f"{p['name']} 摸牌（墙剩 {len(g['deck'])}）")
    if not g["deck"]:
        ok, _ = hu_score(p["hand"], p["melds"], p["que"],
                         gang_count=p["gang_count"], self_draw=True)
        if not ok:
            return _end(g, "牌墙摸完，流局")


def _after_hu(g, seat):
    """血战到底：一家胡了不散桌。返回 True 表示牌局已结束。"""
    if len(alive_seats(g)) <= 1:
        _end(g, "三家已胡，血战结束")
        return True
    return False


def _end(g, reason):
    g["phase"] = "over"
    g["step"] = "over"
    g["claim"] = None
    g["over_reason"] = reason
    g["scores"] = settle(g)
    g["log"].append(f"终局：{reason}")
    return g


def settle(g):
    """终局一次性按番结算：点炮者付给胡家，自摸则其余未胡家各付一份。"""
    scores = {p["seat"]: 0 for p in g["players"]}
    for p in g["players"]:
        if not p["hu"]:
            continue
        fan = p["hu"]["fan"]
        if p["hu"]["self_draw"]:
            # 已经胡过的人不再陪付，只有当时还在场的人分摊
            k = p["hu"].get("order", 0)
            payers = [q["seat"] for q in g["players"]
                      if q["seat"] != p["seat"]
                      and (not q["hu"] or q["hu"].get("order", 0) > k)]
            for s in payers:
                scores[s] -= fan
                scores[p["seat"]] += fan
        else:
            src = p["hu"]["from_seat"]
            scores[src] -= fan
            scores[p["seat"]] += fan
    return scores


# ---------- 笨 bot ----------

def controlled_by_bot(p):
    """bot座位或已因掉线进入托管的真人/API座位。"""
    return p.get("type") == "bot" or p.get("autoplay", False)


def bot_choose(g, seat):
    """返回 (action, tile)。缺门牌优先打，其余打孤张，能胡就胡，不主动碰杠。"""
    p = g["players"][seat]
    if g["phase"] == "que":
        cnt = Counter(t[1] for t in p["hand"])
        men = min(("w", "o", "t"), key=lambda s: cnt.get(s, 0))
        return ("que", men)
    if g["step"] == "claim":
        mine = g["claim"]["eligible"].get(str(seat), [])
        if "hu" in mine:
            return ("hu", None)
        return ("pass", None)
    # 自己的回合
    ok, _ = hu_score(p["hand"], p["melds"], p["que"])
    if ok:
        return ("hu", None)
    que = p["que"]
    que_tiles = [t for t in p["hand"] if t[1] == que]
    if que_tiles:
        # 缺门里先打张数最少的那种
        c = Counter(que_tiles)
        return ("discard", min(que_tiles, key=lambda t: (c[t], -abs(int(t[0]) - 5))))
    return ("discard", _loneliest(p["hand"]))


def _loneliest(hand):
    """孤张评分：同牌张数 + 邻张支持，越低越孤。"""
    c = Counter(hand)
    best, best_score = hand[0], 10 ** 9
    for t in hand:
        n, s = int(t[0]), t[1]
        score = c[t] * 10
        for d in (-2, -1, 1, 2):
            m = n + d
            if 1 <= m <= 9:
                score += c.get(f"{m}{s}", 0) * (3 if abs(d) == 1 else 1)
        score += -abs(n - 5)  # 边张稍微更愿意打
        if score < best_score:
            best, best_score = t, score
    return best


def run_bots(g, limit=400):
    """把所有该 bot 出手的地方跑完。"""
    for _ in range(limit):
        if g["phase"] == "over":
            return g
        actor = None
        if g["phase"] == "que":
            for p in g["players"]:
                if p["que"] is None and controlled_by_bot(p):
                    actor = p["seat"]
                    break
            if actor is None:
                return g
        elif g["step"] == "claim":
            for k in g["claim"]["eligible"]:
                if k not in g["claim"]["responses"] and controlled_by_bot(g["players"][int(k)]):
                    actor = int(k)
                    break
            if actor is None:
                return g
        else:
            p = g["players"][g["turn"]]
            if controlled_by_bot(p) and not p["hu"]:
                actor = p["seat"]
            else:
                return g
        a, t = bot_choose(g, actor)
        act(g, actor, a, t)
    return g


# ---------- 视角 ----------

def view(g, seat):
    """只给 seat 看得到的东西。别家手牌永不出现。"""
    me = g["players"][seat]
    others = []
    for p in g["players"]:
        others.append({
            "seat": p["seat"],
            "name": p["name"],
            "type": p["type"],
            "autoplay": bool(p.get("autoplay", False)),
            "que": p["que"] if (p["seat"] == seat or g["phase"] != "que") else None,
            "hand_count": len(p["hand"]),
            "melds": [{"type": m["type"], "tile": m["tile"],
                       "tiles": meld_tiles(m),
                       "text": " ".join(tile_str(x) for x in meld_tiles(m))} for m in p["melds"]],
            "discards": p["discards"],
            "discards_text": [tile_str(t) for t in p["discards"]],
            "hu": p["hu"],
            "is_me": p["seat"] == seat,
            "is_turn": g["turn"] == p["seat"] and g["step"] == "discard" and g["phase"] == "play",
        })
    waiting = []
    if g["step"] == "claim" and g["claim"]:
        waiting = [int(k) for k in g["claim"]["eligible"] if k not in g["claim"]["responses"]]
    elif g["phase"] == "que":
        waiting = [p["seat"] for p in g["players"] if p["que"] is None]
    elif g["phase"] == "play":
        waiting = [g["turn"]]
    return {
        "phase": g["phase"],
        "step": g["step"],
        "turn": g["turn"],
        "dealer": g["dealer"],
        "wall": len(g["deck"]),
        "my_seat": seat,
        "my_name": me["name"],
        "my_que": me["que"],
        "my_hu": me["hu"],
        "hand": me["hand"],
        "hand_text": [{"t": t, "e": tile_emoji(t), "s": tile_str(t),
                       "n": f"{int(t[0])}{SUITS[t[1]]}"} for t in me["hand"]],
        "melds": [{"type": m["type"], "tile": m["tile"],
                   "text": " ".join(tile_str(x) for x in meld_tiles(m))} for m in me["melds"]],
        "players": others,
        "last_discard": g["last_discard"],
        "last_discard_text": tile_str(g["last_discard"]) if g["last_discard"] else None,
        "last_discard_seat": g["last_discard_seat"],
        "claim_tile": g["claim"]["tile"] if g["claim"] else None,
        "waiting_seats": waiting,
        "available_actions": available_actions(g, seat),
        "log": g["log"][-25:],
        "scores": g["scores"],
        "over_reason": g["over_reason"],
    }
