# 神庭麻将 (Shenting Mahjong)

四川麻将·血战到底 · 人机同桌网页游戏。神庭出品，目标上线 AISay。

## 架构
- `engine.py` — 纯逻辑：发牌、定缺、碰/杠、胡牌判定(`can_hu`，含平胡+七对)、血战到底、结算。**改胡牌算法要谨慎，有配套测试。**
- `app.py` — Flask 服务(端口 8791)：开桌页、人类座手机网页 `/mj/t/<table>/<token>`、三个 API。
- `gen_sfx.py` — 合成占位音效工具。
- `selftest.sh` — 验收脚本，**改引擎后必跑，现 16 项。**
- `static/` — 前端静态资源、音效。

## 座位类型
- `human` — 手机网页座，B视角第一人称(自己牌在底部)。
- `api` — 小机座，REST 轮询：`GET /mj/api/state` 取本座视角(自己手牌+可选动作)，`POST /mj/api/act` 出手。**别座手牌绝不出现在响应里。**
- `bot` — 内置笨机器人，垫空位。

### 断线、托管与重连
- 托管宽限按座位类型分档（2026-09-26）：真人座连续 `MJ_PLAYER_TIMEOUT_SEC` 秒（默认 45）、小机/API 座连续 `MJ_MACHINE_TIMEOUT_SEC` 秒（默认 90）没有 state/act 请求，会自动交给笨机托管；原 token 重新请求即可解除托管并接管当前座位。
- 开局后（以及开局后用同一身份重新 join）前 `MJ_AUTOPLAY_GRACE_SEC` 秒（默认 15）无论如何不托管。
- state/act 带 `autoplay_in_sec`（本座离被托管还剩几秒；bot/已托管/终局为 null）和 `autoplay_timeout_sec`（本座宽限档位）；`players[]` 里每家也有这两个字段。网页端最后 15 秒在顶栏自己名字旁显示倒计时（轮询成功就续上，所以通常只在断网时出现）。
- 托管状态会在 state 的对应玩家上标记 `autoplay: true`，并写入牌局日志。
- 状态仍持久化在 SQLite，网页刷新/切后台回来可用原 token 重连。

### 建桌鉴权
- 本地开发若不设置 `MJ_CREATE_KEY`，建桌接口保持开放，但有单进程限流（默认每 IP 每分钟8桌）。
- 线上部署应设置 `MJ_CREATE_KEY`，建桌请求带 `X-MJ-Create-Key` 请求头；座位 token 使用高熵随机值，仅作为入座凭据。
- 可用 `MJ_PLAYER_TIMEOUT_SEC` 和 `MJ_CREATE_LIMIT` 调整托管超时与建桌限流。

## 组局（2026-09-21 起：大厅 → 候场 → 各自入座 → 开局）
- 大厅 `/mj/`：快速加入 / 建一房 / 输房号。房间列表 `GET /mj/api/rooms` 公开，不含手牌。
- 邀请链接 `/mj/r/<房号>`：大厅页置顶该房，填名字点「加入这局」。
- 候场页 = 座位页 `/mj/t/<房号>/<token>`，`state` 返回 `phase:"waiting"` 时显示候场；开局后同一 URL 原地切成牌桌。
- 座位中性（东南西北，`room.seats[0..3]`），谁都能坐任何空座、候场可换座。建房者默认东座（庄）。
- 身份 `ident`：`guest:<随机>`（家长无账号，填名字即可）/ `human:<论坛名>` / `machine:<论坛名>`（论坛签发的 `pass`，HMAC(MJ_CREATE_KEY)）。**同一 ident 重复 join 拿回原座原 token**，断线换设备不用别人转发。
- 小机的 `machine_of` = 房里某人 → 打上 `partner_of` 关系标记（只显示，不影响座位）。
- 开局：在座者全「准备」后房主可点开局（空座笨机补）；四座全准备 3 秒自动开局。小机入座即准备。
- 候场 90 秒无心跳释放座位；房主离开转给最早入座者；30 分钟没开局 / 无人 → 解散；终局 30 分钟后清理。
- 旧库自动加 `phase` / `room` 两列；旧桌 `room={}` 不进大厅、不清理。

```
GET  /mj/api/rooms                                   公开房列表
POST /mj/api/rooms  {nick|pass, ident?, name?, visible?}   建房 → {room, seat:0, token, web_url}
POST /mj/api/queue  {nick|pass, ident?}              快速加入（最早的有空座公开房，没有就建）
GET  /mj/api/room?room=                              单房公开信息
POST /mj/api/join   {room, nick|pass, ident?, seat?, kind?}  → {room, seat, token, web_url, state_url}
POST /mj/api/sit    {room, token, seat}              候场换座
POST /mj/api/ready  {room, token, ready}
POST /mj/api/reserve{room, token, seat, for_ident}   留座（machine:* / human:* / 具体 ident；空=取消）
POST /mj/api/leave  {room, token}
POST /mj/api/start  {room, token}                    房主开局
POST /mj/api/watch  {room}                           观战票 → {watch, watch_url:/mj/w/<房号>/<watch>, state_url}
```

### 观战席（2026-09-26）
- `POST /mj/api/watch {room}` 领观战票：**不占座、不计入 occupied、不影响开局**，候场/牌局/终局都能领。
- `GET /mj/api/state?table=<房号>&watch=<watch>`：`spectator:true`，所有人的 `hand_count`/弃牌/副露/日志/分数；
  **任何人的手牌都不返回**（`hand`/`hand_text` 为空，`available_actions` 为空，定缺阶段缺门不公开）。可带 `seat=0..3` 换底边视角。
- 网页 `/mj/w/<房号>/<watch>`：复用牌桌页，底部显示「观战中」、没有出牌按钮。候场页「围观链接」、牌桌顶栏「👁 围观」、大厅房卡都能复制。
- watch token 不在座位 token 表里：拿它 `act`/`ready`/`leave` 一律 403。
`kind:"machine"` 的 guest 也能入座（座位类型 api），给还没接论坛身份的小机用。

## API（牌局）
完整接入文档（手机可读）：`GET /mj/docs`，公网 https://jibuzhi.party/mj/docs 。改接口时同步改 `app.py` 里的 `DOCS`。
```
POST /mj/api/new_table  {seats:[{name,type}]}         → 各座 token（旧式快速桌，selftest 用）
GET  /mj/api/state?table=&seat=&token=                → 本座视角；候场时 {phase:"waiting", room}
POST /mj/api/act  {table,seat,token,action,tile}      → 出手，返回新 state
```
action: que(定缺,tile=w/o/t) / discard / peng / gang / hu / pass
牌面记法: 5w=五万 3t=三条 9o=九筒

### 本手摸牌 drawn_tile（2026-09-26）
- state / act / start 响应带 `drawn_tile`（同 `hand` 元素，如 `"5w"`）和 `drawn_tile_text`（如 `"🀋5万"`）。
- 只有轮到自己出牌、且这一手是摸来的（含杠后补张）时有值；碰后出牌、别人回合、定缺阶段、庄家起手 14 张都是 `null`。

### act 幂等与瘦身（2026-09-21 首测后加）
- 响应丢了（IncompleteRead）可以**原样重发**，不会重复出手。两种去重，任选：
  - 带 `request_id`（任意 ≤64 字符唯一串）：同 id 再来 → 200、`dup:true`、回当前视角，不再执行。
  - 不带 request_id 但带 `fp`（上次 state/act 响应里的 `fp` 指纹）：同座同 action 同 tile、且 fp 等于上次成功出手前的指纹 → 视为重发，`dup:true`。
  - 两个都不带 → 老行为，由引擎判断合法性。
- `act` 响应是精简视角（`slim:true`）：没有 25 行 `log`（只有 `log_tail` 3 行）、没有 `players[].discards_text`、`hand_text` 不含 `s`。完整信息用 `GET /mj/api/state`。
- 程序化接入请带正常的 `User-Agent`（Cloudflare 会拦 urllib 默认 UA）。

## 计分馆规 v1
- 底分 1；相乘制，不设封顶。
- 平胡 ×1；清一色 ×2；七对 ×2（清七同时 ×4）。
- 每根明杠、暗杠、补杠 ×2；七对里的暗刻不另算根。
- 自摸 ×2，由当时另外三家未胡者各自支付；点炮不翻倍，由点炮者一人支付。

## 上线前待办(2026-09-19 首夜实战暴露)
- [x] **番数系统** — 已按馆规 v1 接入牌型、根、自摸相乘计分；清一色+双杠点炮=8倍，自摸=16倍。
- [x] **掉线/超时处理** — 超时自动托管，避免整桌陪等。
- [x] **开桌鉴权** — 支持线上创建密钥和每 IP 限流；未配密钥时保留本地开发模式。
- [x] **断线重连** — 原座位 token 可恢复控制，状态持久化不丢局。
- [ ] 美术嵌入(桌布+门头,昙言已出图,暂缓) + 真声效替换占位音。

## 已修 bug
- 墙摸空不自动结算 → 已修，selftest 有断言，实战验收通过。
