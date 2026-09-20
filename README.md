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
- 真人/API 座位连续 `MJ_PLAYER_TIMEOUT_SEC` 秒（默认45秒）没有 state/act 请求，会自动交给笨机托管；原 token 重新请求即可解除托管并接管当前座位。
- 托管状态会在 state 的对应玩家上标记 `autoplay: true`，并写入牌局日志。
- 状态仍持久化在 SQLite，网页刷新/切后台回来可用原 token 重连。

### 建桌鉴权
- 本地开发若不设置 `MJ_CREATE_KEY`，建桌接口保持开放，但有单进程限流（默认每 IP 每分钟8桌）。
- 线上部署应设置 `MJ_CREATE_KEY`，建桌请求带 `X-MJ-Create-Key` 请求头；座位 token 使用高熵随机值，仅作为入座凭据。
- 可用 `MJ_PLAYER_TIMEOUT_SEC` 和 `MJ_CREATE_LIMIT` 调整托管超时与建桌限流。

## API
```
POST /mj/api/new_table  {seats:[{name,type}]}         → 各座 token
GET  /mj/api/state?table=&seat=&token=                → 本座视角
POST /mj/api/act  {table,seat,token,action,tile}      → 出手，返回新 state
```
action: que(定缺,tile=w/o/t) / discard / peng / gang / hu / pass
牌面记法: 5w=五万 3t=三条 9o=九筒

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
