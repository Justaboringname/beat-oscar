"""Duolingo 象棋协议层。

实测出来的协议(2026-08-15，duolingo.cn 网页版)：

  POST /chess/1/<uid>/matches/<matchId>
    {"fen": <走子前FEN>, "move": <UCI>, "moveHistory": [...含本步],
     "fromLanguage": "zh"}
  ->
    {"match": {"boardFen": <奥斯卡回招后的FEN>,
               "moveHistory": [...含奥斯卡回招],
               "playerColor": "white"|"black",
               "status": "active"|...,
               "outcome": ..., "endCondition": ...},
     "opponentMove": <UCI>}

一次请求 = 我方走一步 + 拿到对手回招 + 拿到新局面。没有 WebSocket，没有轮询。

实测到的终局取值(2026-08-15 一局将杀)：
    status="completed"  outcome="black"  endCondition="checkmate"
判断结束一律用 `status != "active"`，别去 pattern match 这些值 —— 认输/超时/
和棋的取值都还没观察到。

⚠️ 核心不变量：`fen` 与 `moveHistory` 是**服务端授权的值**，只能原样回传。
   绝不用 python-chess 自行生成 —— 易位权/半步钟只要差一个字符就会静默失步。
   python-chess 只用来算着法，不用来造状态。
"""

from __future__ import annotations

import time

from bridge import run_js


# ---------------------------------------------------------------- 提交走子

_SUBMIT = r"""
var x = new XMLHttpRequest();
// 实测服务端 Allow: HEAD, OPTIONS, PATCH, GET —— 走子是 PATCH 不是 POST
x.open('PATCH', P.url, false);             // 同步：AppleScript 桥不 await Promise
x.setRequestHeader('Content-Type', 'application/json');
try { x.send(JSON.stringify(P.body)); }
catch (e) { return {ok: false, stage: 'send', err: String(e)}; }
if (x.status < 200 || x.status >= 300) {
  return {ok: false, stage: 'http', status: x.status, body: String(x.responseText).substring(0, 600)};
}
var j;
try { j = JSON.parse(x.responseText); }
catch (e) { return {ok: false, stage: 'parse', body: String(x.responseText).substring(0, 600)}; }
return {ok: true, data: j};
"""


def submit_move(uid: str, match_id: str, fen: str, move: str,
                move_history: list, from_language: str = "zh") -> dict:
    """提交一步棋，返回服务端响应(含对手回招)。

    fen / move_history 必须来自服务端上一轮的返回值，不可自行构造。
    """
    body = {
        "fen": fen,
        "fromLanguage": from_language,
        "move": move,
        "moveHistory": list(move_history) + [move],
    }
    res = run_js(_SUBMIT, params={
        "url": f"/chess/1/{uid}/matches/{match_id}",
        "body": body,
    })
    if not res.get("ok"):
        raise RuntimeError(f"提交走子失败: {res}")
    return res["data"]


# ------------------------------------------------- 从 React fiber 只读当前局面

_READ_STATE = r"""
var root = document.getElementById('root');
if (!root) return {err: 'no root'};
var key = Object.keys(root).filter(function (k) { return k.indexOf('__reactContainer') === 0; })[0];
if (!key) return {err: 'no fiber key'};
var best = null, stack = [root[key]], seen = 0;
while (stack.length && seen < 60000) {
  var f = stack.pop(); seen++;
  if (!f || typeof f !== 'object') continue;
  try {
    var h = f.memoizedState, d = 0;
    while (h && d < 40) {
      var s = h.memoizedState;
      if (s && typeof s === 'object' && s.match && s.match.boardFen && s.id) {
        best = {id: s.id, fen: s.match.boardFen, hist: s.match.moveHistory || [],
                color: s.match.playerColor, status: s.match.status,
                outcome: s.match.outcome, endCondition: s.match.endCondition,
                opponentMove: s.opponentMove || null};
      }
      h = h.next; d++;
    }
  } catch (e) {}
  if (f.child) stack.push(f.child);
  if (f.sibling) stack.push(f.sibling);
}
return best || {err: 'no match state'};
"""


def read_state() -> dict:
    """从 React fiber 只读当前对局状态。只读，不改任何 React 内部。"""
    st = run_js(_READ_STATE)
    if st.get("err"):
        raise RuntimeError(f"读取对局状态失败: {st['err']}（页面上有在下的棋吗？）")
    return st


# ------------------------------------------------- 合成点击落子

_GEOMETRY = r"""
var divs = document.querySelectorAll('div');
for (var i = 0; i < divs.length; i++) {
  var d = divs[i].getBoundingClientRect();
  if (d.width > 400 && Math.abs(d.width - d.height) < 8) {
    return {left: d.left, top: d.top, size: d.width};
  }
}
return {err: 'board not found'};
"""


def board_geometry() -> dict:
    """棋盘方形区的视口坐标。每次走子前重新取，窗口缩放也不会错位。"""
    g = run_js(_GEOMETRY)
    if g.get("err"):
        raise RuntimeError("找不到棋盘（页面可能不在对局中）")
    return g


def square_to_xy(square: str, geo: dict, player_color: str) -> tuple[float, float]:
    """代数格名(如 'e2') -> 视口坐标。执黑时棋盘上下翻转。"""
    file_idx = ord(square[0]) - ord("a")      # a..h -> 0..7
    rank_idx = int(square[1]) - 1             # 1..8 -> 0..7
    cell = geo["size"] / 8.0
    if player_color == "white":
        col, row = file_idx, 7 - rank_idx     # 白在下：rank1 在最下一行
    else:
        col, row = 7 - file_idx, rank_idx     # 黑在下：整盘翻转
    return (geo["left"] + (col + 0.5) * cell,
            geo["top"] + (row + 0.5) * cell)


_CLICK = r"""
var el = document.elementFromPoint(P.x, P.y);
if (!el) return {ok: false, err: 'elementFromPoint null'};
var fire = function (type, Ctor, extra) {
  var init = {bubbles: true, cancelable: true, composed: true,
              clientX: P.x, clientY: P.y, button: 0,
              buttons: (type.indexOf('up') > -1 ? 0 : 1), view: window};
  for (var k in (extra || {})) init[k] = extra[k];
  try { el.dispatchEvent(new Ctor(type, init)); return true; } catch (e) { return false; }
};
var pe = {pointerId: 1, pointerType: 'mouse', isPrimary: true};
fire('pointerdown', window.PointerEvent, pe);
fire('mousedown',   window.MouseEvent,   {});
fire('pointerup',   window.PointerEvent, pe);
fire('mouseup',     window.MouseEvent,   {});
fire('click',       window.MouseEvent,   {});
return {ok: true, target: el.tagName};
"""


def click_at(x: float, y: float) -> dict:
    """在视口坐标处合成一次完整点击。不需要 Safari 在前台。"""
    return run_js(_CLICK, params={"x": x, "y": y})


def play_move_by_click(uci: str, player_color: str, pause: float = 0.7) -> dict:
    """用合成点击走一步：先点起点格，再点终点格。

    走的是页面自己的走子逻辑，所以 store / UI / 网络请求全都由页面负责，
    不会出现「服务端走了但页面不知道」的状态分裂。

    ⚠️ `pause` 别调小。实测 0.25s 时选中动画还没播完，第二下点击会被整个吃掉 ——
    表现为棋子停在选中态、走子不落。0.25s 下曾连走 19 步只错 1 步，是踩在边缘上；
    0.7s 是把这个边缘让开，不是随便取的数。
    """
    geo = board_geometry()
    src, dst = uci[0:2], uci[2:4]
    x1, y1 = square_to_xy(src, geo, player_color)
    r1 = click_at(x1, y1)
    time.sleep(pause)
    x2, y2 = square_to_xy(dst, geo, player_color)
    r2 = click_at(x2, y2)
    return {"src": src, "dst": dst, "from": r1, "to": r2}


_GET_UID = r"""
var hits = [], es = performance.getEntriesByType('resource');
for (var i = 0; i < es.length; i++) {
  // 只认 chess 专用端点：/users/ 这类通用路径里会混进好友 uid
  var m = String(es[i].name).match(/\/chess\/1\/(\d{6,})\//);
  if (m && hits.indexOf(m[1]) < 0) hits.push(m[1]);
}
return hits;
"""


_MATCH_ID_FROM_PERF = r"""
var hits = [], es = performance.getEntriesByType('resource');
for (var i = 0; i < es.length; i++) {
  var m = String(es[i].name).match(/\/chess\/1\/\d+\/matches\/(bot\|[^\/?#]+)/);
  if (m && hits.indexOf(m[1]) < 0) hits.push(m[1]);
}
return hits;
"""


def get_match_id() -> str:
    """当前对局 id。

    fiber 是这套东西里最脆的依赖(页面渲染到一半、Duolingo 改版都可能让它读不到)，
    所以再挂一条退路：从已发请求的 URL 里捞。退路只在走过至少一步后才有数据，
    两条都空才报错。
    """
    try:
        return read_state()["id"]
    except Exception:
        pass
    hits = run_js(_MATCH_ID_FROM_PERF)
    if hits:
        return hits[-1]
    raise RuntimeError("拿不到对局 id：页面上有正在下的棋吗？(fiber 和请求记录都没有)")


def get_uid() -> str:
    """当前用户 uid。

    ⚠️ 只从 /chess/1/<uid>/ 解析。通用的 /users/<id> 路径里会出现好友 uid
    (friendButtonAvatarUserId)，用错会打到别人身上。
    """
    hits = run_js(_GET_UID)
    if not hits:
        raise RuntimeError("拿不到 uid：页面还没发过 chess 请求，刷新后重开一局再试")
    if len(hits) > 1:
        raise RuntimeError(f"chess 端点里出现多个 uid {hits}，不敢猜，停机")
    return hits[0]


_SERVER_STATE = r"""
var x = new XMLHttpRequest();
x.open('GET', '/chess/1/' + P.uid + '/matches/' + P.matchId, false);
x.send(null);
if (x.status < 200 || x.status >= 300) {
  return {err: 'HTTP ' + x.status, body: String(x.responseText).substring(0, 300)};
}
var j;
try { j = JSON.parse(x.responseText); } catch (e) { return {err: 'parse'}; }
var m = j.match || j;
if (!m || !m.boardFen) return {err: 'no boardFen', keys: Object.keys(j).join(',')};
return {id: m.id, fen: m.boardFen, hist: m.moveHistory || [], color: m.playerColor,
        status: m.status, outcome: m.outcome, endCondition: m.endCondition};
"""


def server_state(uid: str, match_id: str) -> dict:
    """向服务端要当前权威局面。

    这是唯一可信的实时来源：fiber 里 challenge.match.boardFen 是关卡初始快照、
    走子中不更新，challengeStates.guess.moveHistory 只记己方着法。
    """
    st = run_js(_SERVER_STATE, params={"uid": uid, "matchId": match_id})
    if st.get("err"):
        raise RuntimeError(f"读取服务端局面失败: {st}")
    return st


def wait_for_advance(uid: str, match_id: str, prev_len: int,
                     timeout: float = 40.0, poll: float = 0.6) -> dict:
    """等这一步和奥斯卡的回招都落到服务端。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        st = server_state(uid, match_id)
        last = st
        if st.get("status") != "active":
            return st
        if len(st.get("hist", [])) > prev_len:
            return st
        time.sleep(poll)
    return last or {}


_CLEAR_SEL = r"""
// 点棋盘外的页面空白，把可能残留的选中态清掉。
// 不能点棋盘格：万一那格正好是选中棋子的合法目标，就会走出一步乱棋。
var el = document.elementFromPoint(P.x, P.y);
if (!el) return {ok: false};
if (el.tagName === 'CANVAS') return {ok: false, err: 'refuse: 落在棋盘上'};
var fire = function (t, C) {
  try {
    el.dispatchEvent(new C(t, {bubbles: true, cancelable: true, composed: true,
                               clientX: P.x, clientY: P.y, button: 0, view: window}));
  } catch (e) {}
};
fire('pointerdown', window.PointerEvent); fire('mousedown', window.MouseEvent);
fire('pointerup', window.PointerEvent);   fire('mouseup', window.MouseEvent);
fire('click', window.MouseEvent);
return {ok: true, target: el.tagName};
"""


def clear_selection(geo: dict | None = None) -> dict:
    """清掉棋盘上可能残留的选中态。"""
    geo = geo or board_geometry()
    x = max(20.0, geo["left"] / 2.0)          # 棋盘左侧的页面空白
    y = geo["top"] + geo["size"] / 2.0
    return run_js(_CLEAR_SEL, params={"x": x, "y": y})


def play_move_with_retry(uci: str, color: str, uid: str, match_id: str,
                         prev_len: int, retries: int = 3,
                         settle: float = 0.6) -> tuple[dict, int]:
    """走一步，没落下就重试。

    实测原因：奥斯卡回招后页面还在播动画，那一瞬间的点击会被吃掉。
    重试前先等一下并清掉残留选中态。返回 (最新状态, 实际尝试次数)。
    """
    st = {}
    for attempt in range(1, retries + 1):
        if attempt > 1:
            clear_selection()
            time.sleep(1.2)
        else:
            time.sleep(settle)
        play_move_by_click(uci, color)
        st = wait_for_advance(uid, match_id, prev_len, timeout=18.0)
        if len(st.get("hist", [])) > prev_len or st.get("status") != "active":
            return st, attempt
    return st, retries
