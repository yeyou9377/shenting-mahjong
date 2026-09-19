#!/usr/bin/env bash
# 川麻 v0 自测：用 curl 模拟 4 个 API 座位打完整局。
# 验证：定缺流转 / 碰杠改变轮转 / 一家胡后血战继续 / 墙尽终局结算 / 座位视角隔离
#
#   用法: ./selftest.sh [BASE]     默认 BASE=http://127.0.0.1:8791
set -u
BASE="${1:-http://127.0.0.1:8791}"
# 本机自测必须绕开 http_proxy，否则每个请求都会卡在代理上
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY all_proxy 2>/dev/null
mj(){ curl -s --noproxy '*' --max-time 15 "$@"; }
PASS=0; FAIL=0
ok(){ echo "  ✅ $1"; PASS=$((PASS+1)); }
no(){ echo "  ❌ $1"; FAIL=$((FAIL+1)); }

# 出牌策略（jq）：缺门牌先走，其余打孤张——同牌和邻张越少越孤
read -r -d '' PICK <<'JQ'
def sc($c): . as $t | ($t[0:1]|tonumber) as $n | ($t[1:2]) as $s |
  ($c[$t]//0)*10
  + (($c[(($n-1)|tostring)+$s]//0)*3) + (($c[(($n+1)|tostring)+$s]//0)*3)
  + ($c[(($n-2)|tostring)+$s]//0)     + ($c[(($n+2)|tostring)+$s]//0);
(reduce .hand[] as $t ({}; .[$t]=(.[$t]//0)+1)) as $c
| ([.hand[]|select(.[1:2]==$q)]) as $ques
| (if ($ques|length)>0 then $ques else .hand end)
| map({t:., v:(.|sc($c))}) | min_by(.v) | .t
JQ

tok(){ case $1 in 0) echo "$TK0";; 1) echo "$TK1";; 2) echo "$TK2";; 3) echo "$TK3";; esac; }
state(){ mj "$BASE/mj/api/state?table=$TABLE&seat=$1&token=$(tok $1)"; }
act(){ mj -X POST "$BASE/mj/api/act" -H 'Content-Type: application/json' \
       -d "{\"table\":\"$TABLE\",\"seat\":$1,\"token\":\"$(tok $1)\",\"action\":\"$2\",\"tile\":$3}"; }

open_table(){
  NEW=$(mj -X POST "$BASE/mj/api/new_table" -H 'Content-Type: application/json' \
    -d '{"seats":[{"name":"灯下A","type":"api"},{"name":"灯下B","type":"api"},
                  {"name":"灯下C","type":"api"},{"name":"灯下D","type":"api"}]}')
  TABLE=$(echo "$NEW" | jq -r '.table')
  [ "$TABLE" = "null" ] && { echo "$NEW"; exit 1; }
  for i in 0 1 2 3; do eval "TK$i=$(echo "$NEW" | jq -r ".seats[$i].token")"; done
}

play_table(){   # 定缺 + 打完 -> 设置 PHASE/WALL/PENG/GANG/HU_COUNT/FIRST_HU_WALL/SUM/REASON
  for i in 0 1 2 3; do
    Q=$(state $i | jq -r '.available_actions[]? | select(.action=="que") | .options[0]')
    [ -n "$Q" ] && act $i que "\"$Q\"" > /dev/null
  done
  QUE_PHASE=$(state 0 | jq -r '.phase')
  QUE_N=$(state 0 | jq -r '[.players[].que] | map(select(. != null)) | length')

  FIRST_HU_WALL=""; HU_COUNT=0; ROUNDS=0; REJECTS=0
  while [ $ROUNDS -lt 900 ]; do
    ROUNDS=$((ROUNDS+1))
    [ "$(state 0 | jq -r '.phase')" = "over" ] && break
    MOVED=0
    for i in 0 1 2 3; do
      V=$(state $i)
      A=$(echo "$V" | jq -c '.available_actions')
      [ "$A" = "[]" ] && continue
      MOVED=1
      QUE=$(echo "$V" | jq -r '.my_que')
      if   echo "$A" | jq -e '.[]|select(.action=="hu")' >/dev/null; then ACT=hu; T=null
      elif echo "$A" | jq -e --arg q "$QUE" \
             '.[]|select(.action=="gang")|.tiles[]|select(.[1:2]!=$q)' >/dev/null; then
           ACT=gang; T=$(echo "$A" | jq -c --arg q "$QUE" \
             '[.[]|select(.action=="gang")|.tiles[]|select(.[1:2]!=$q)][0]')
      elif echo "$A" | jq -e --arg q "$QUE" \
             '.[]|select(.action=="peng")|.tiles[]|select(.[1:2]!=$q)' >/dev/null; then
           ACT=peng; T=$(echo "$A" | jq -c --arg q "$QUE" \
             '[.[]|select(.action=="peng")|.tiles[]|select(.[1:2]!=$q)][0]')
      elif echo "$A" | jq -e '.[]|select(.action=="discard")' >/dev/null; then
           ACT=discard; T=$(echo "$V" | jq -c --arg q "$QUE" "$PICK")
      else ACT=pass; T=null
      fi
      R=$(act $i "$ACT" "$T")
      if [ "$(echo "$R" | jq -r '.ok')" != "true" ]; then
        REJECTS=$((REJECTS+1))
        echo "  座位 $i 动作 $ACT $T 被拒: $(echo "$R" | jq -r '.error')"
      fi
      NH=$(echo "$R" | jq -r '[.players[]?|select(.hu!=null)]|length // 0')
      if [ "${NH:-0}" -gt "$HU_COUNT" ] 2>/dev/null; then
        HU_COUNT=$NH
        [ -z "$FIRST_HU_WALL" ] && FIRST_HU_WALL=$(echo "$R" | jq -r '.wall')
      fi
    done
    [ $MOVED -eq 0 ] && { echo "  没人能动，卡住了"; break; }
  done

  FIN=$(state 0)
  PENG=$(echo "$FIN"  | jq -r '[.players[].melds[]|select(.type=="peng")]|length')
  GANG=$(echo "$FIN"  | jq -r '[.players[].melds[]|select(.type|startswith("gang"))]|length')
  WALL=$(echo "$FIN"  | jq -r '.wall')
  PHASE=$(echo "$FIN" | jq -r '.phase')
  REASON=$(echo "$FIN" | jq -r '.over_reason')
  SUM=$(echo "$FIN"   | jq -r '[.scores|to_entries[].value]|add')
}

echo "== 1. 建桌（4 个 API 座位）=="
open_table
echo "  桌号 $TABLE"
[ -n "$TABLE" ] && ok "建桌成功，拿到 4 个 token" || no "建桌失败"

echo "== 2. 座位视角隔离 =="
LEAK=$(state 0 | jq -r '[.players[] | select(.is_me==false) | keys[]] | index("hand") // "none"')
[ "$LEAK" = "none" ] && ok "别家视角里没有 hand 字段" || no "泄露了别家手牌"
RAW=$(state 0)
MYN=$(echo "$RAW" | jq -r '.hand|length')
ALLN=$(echo "$RAW" | jq -r '[.players[].hand_count]|add')
[ "$MYN" -lt "$ALLN" ] && ok "只看得到自己的 $MYN 张（全桌 $ALLN 张）" || no "看到的牌太多了"
BADTOK=$(mj "$BASE/mj/api/state?table=$TABLE&seat=1&token=$TK0" | jq -r '.ok')
[ "$BADTOK" = "false" ] && ok "拿 0 号 token 读 1 号座位被拒" || no "token 校验没生效"

echo "== 3~4. 定缺 + 打整局（最多开 6 桌，直到出现至少一家胡）=="
ATTEMPT=0
while :; do
  ATTEMPT=$((ATTEMPT+1))
  play_table
  echo "  第 $ATTEMPT 桌 [$TABLE]：回合 $ROUNDS | 墙剩 $WALL | 碰 $PENG | 杠 $GANG | 胡 $HU_COUNT 家 | $REASON"
  [ "$HU_COUNT" -gt 0 ] && break
  [ $ATTEMPT -ge 6 ] && break
  open_table
done

echo "== 5. 断言 =="
[ "$QUE_PHASE" = "play" ] && ok "4 家定缺后自动进入 play（定缺流转）" || no "定缺后阶段是 $QUE_PHASE"
[ "$QUE_N" = "4" ] && ok "4 家的缺门都记上了" || no "只有 $QUE_N 家定了缺"
[ "$REJECTS" = "0" ] && ok "整局没有一个动作被引擎拒绝" || no "有 $REJECTS 个动作被拒"
[ "$PHASE" = "over" ] && ok "牌局走到 over" || no "牌局停在 $PHASE"
if [ "$WALL" = "0" ] || [ "$HU_COUNT" -ge 3 ]; then
  ok "终局条件成立：$REASON"
else
  no "既没摸完也没三家胡（墙剩 $WALL，胡 $HU_COUNT 家）"
fi
[ "$SUM" = "0" ] && ok "终局一次性结算，积分守恒（和为 0）" || no "积分和是 $SUM"
[ "$((PENG+GANG))" -gt 0 ] && ok "出现过碰/杠（轮转被改变）" || no "整局没碰没杠"
if [ "$HU_COUNT" -gt 0 ] && [ -n "$FIRST_HU_WALL" ] && [ "$FIRST_HU_WALL" -gt "$WALL" ]; then
  ok "血战到底：第一家胡时墙剩 ${FIRST_HU_WALL}，没散桌，继续打到 ${WALL}"
elif [ "$HU_COUNT" -ge 3 ]; then
  ok "血战到底：打到三家胡才收"
else
  no "血战延续没验到（胡 $HU_COUNT 家，首胡时墙剩 ${FIRST_HU_WALL:-?}，终局墙剩 $WALL）"
fi
echo "  番型: $(mj "$BASE/mj/api/state?table=$TABLE&seat=0&token=$TK0" \
        | jq -c '[.players[]|select(.hu!=null)|{name,fan:.hu.fan,flags:.hu.flags}]')"
echo "  分数: $(state 0 | jq -c '.scores')"

echo "== 6. 人类座网页 =="
HTML=$(mj "$BASE/mj/t/$TABLE/$TK0")
echo "$HTML" | grep -q 'id="table"' && ok "网页返回牌桌 HTML" || no "网页没返回"
echo "$HTML" | grep -q "$TK0" && ok "网页里注入了本座 token" || no "token 没注入"
CODE=$(mj -o /dev/null -w '%{http_code}' "$BASE/mj/t/$TABLE/wrongtoken")
[ "$CODE" = "404" ] && ok "错 token 进桌 404" || no "错 token 返回 $CODE"

echo "== 7. 墙尽自动结算（引擎单测：摸到最后一张即终局，无需额外 discard）=="
if PYTHONPATH="$(dirname "$0")" python3 - <<'PY'
import sys, engine as E

# A) 摸走最后一张、且摸牌者不能自摸胡 -> 立即 phase=over，不留 discard
g = E.new_game([{"name":"A"},{"name":"B"},{"name":"C"},{"name":"D"}], seed=7)
g["phase"], g["step"] = "play", "discard"
for p in g["players"]:
    p["que"] = None
g["deck"] = ["5o"]                       # 墙里只剩一张
g["players"][1]["melds"] = []
g["players"][1]["hand"] = E.sort_tiles(
    ["1w","4w","7w","2w","5w","8w","2t","5t","8t","1o","3o","6o","9o"])  # 13 张散牌，加任何牌都胡不了
E._draw_to(g, 1)                         # B 摸走最后一张
assert g["phase"] == "over", f"墙尽后 phase 应为 over，实为 {g['phase']}"
assert g["step"] == "over", f"墙尽后 step 应为 over，实为 {g['step']}"
assert not E.available_actions(g, 1), "墙尽终局后不应还有 discard 等动作"
assert g["over_reason"], "终局应带 over_reason"
assert g["scores"] is not None, "终局应已结算 scores"

# B) 摸走最后一张但摸牌者能自摸胡 -> 保留 play/discard，给他 hu 的机会
g2 = E.new_game([{"name":"A"},{"name":"B"},{"name":"C"},{"name":"D"}], seed=9)
g2["phase"], g2["step"] = "play", "discard"
for p in g2["players"]:
    p["que"] = None
g2["deck"] = ["5o"]
g2["players"][2]["melds"] = []
# 摸到 5o 后成 1w1w1w + 2t3t4t + 5o6o7o + 2o3o4o + 5o5o(对) -> 胡
g2["players"][2]["hand"] = E.sort_tiles(
    ["1w","1w","1w","2t","3t","4t","5o","6o","7o","2o","3o","4o","5o"])
E._draw_to(g2, 2)
assert g2["phase"] == "play", f"能自摸胡时不应终局，phase={g2['phase']}"
assert g2["step"] == "discard"
assert any(a["action"] == "hu" for a in E.available_actions(g2, 2)), "能自摸胡时应给出 hu 动作"
print("engine draw-to-empty transitions OK")
PY
then ok "摸到最后一张自动终局（不能自摸胡时无需额外 discard），能自摸胡时保留出手"
else no "墙尽自动结算断言未通过"; fi

echo
echo "通过 $PASS 项，失败 $FAIL 项"
[ $FAIL -eq 0 ] || exit 1
