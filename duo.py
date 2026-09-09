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

from bridge import BridgeError, run_js


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
// 找棋盘 canvas：>300px 是为了甩掉奥斯卡头像那块 240x240 的角色 canvas
var cs = document.querySelectorAll('canvas'), big = null, area = 0;
for (var i = 0; i < cs.length; i++) {
  var r = cs[i].getBoundingClientRect();
  if (r.width > 300 && r.width * r.height > area) { area = r.width * r.height; big = cs[i]; }
}
if (!big) return {err: 'no board canvas'};
var c = big.getBoundingClientRect();
var pe = big.parentElement;
if (!pe) return {err: 'canvas has no parent'};
var p = pe.getBoundingClientRect();
var box = function (r) { return {w: Math.round(r.width), h: Math.round(r.height),
                                 l: Math.round(r.left), t: Math.round(r.top)}; };
// canvas 比棋盘高(上方留给奥斯卡溢出)，棋盘本身锚在父容器上沿、宽度=父容器宽
if (!(p.width > 300 && Math.abs(p.width - c.width) < 4 && p.height >= p.width)) {
  return {err: 'parent geometry mismatch', canvas: box(c), parent: box(p)};
}
return {left: p.left, top: p.top, size: p.width};
"""


def board_geometry() -> dict:
    """棋盘方形区的视口坐标。每次走子前重新取，窗口缩放也不会错位。

    ⚠️ 2026-09-09 实测：棋盘早已不是 DOM 方格，而是**一整块 canvas**，且这块
    canvas 不是方的(实测 748x935 —— 上方多出来的高度留给奥斯卡的角色动画溢出)。
    老启发式「div + width>400 + 近方形」于是永远匹配不到，报的却是「页面可能不在
    对局中」，把版面变更误导成页面状态问题。

    现在的取法：最大的那块 canvas(>300px，甩掉 240x240 的头像 canvas)，棋盘 =
    它**父容器**的左上角 + 父容器宽度当边长。实测校准(2560x1440 视口)：
    真实格网原点 (906.5, 304.2)、边长 742.5、格宽 92.81；本法给出 (906, 300, 748)，
    最坏一格中心偏差 ~4.7px —— 相对 93px 的格子完全够用。
    """
    g = run_js(_GEOMETRY)
    if g.get("err"):
        raise RuntimeError(
            f"找不到棋盘：{g}\n"
            "（页面不在对局中，或者多邻国又改版了 —— 上面的 canvas/parent 尺寸就是现场证据）"
        )
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


# ------------------------------------ 走页面自己的落子入口(不合成点击,首选)

# 棋盘是 Rive 画的,64 个格子都不是 DOM 元素。真实点击的归宿是控制器的
# onRiveEventReceived: 它拿 Rive 事件里的 row/col,去 validMovesForSelectedPosition
# 里匹配出 move 对象,再调 makeMoveOnRiveBoard。我们直接从这一步接进去,
# 于是命中测试、选中动画、点击时序全都绕开了,而下游(引擎/UI/网络请求)一个不少。
#
# 实测(2026-09-09):这条路服务端照单全收 —— 1 步 -> 3 步,奥斯卡当场回招 d2d4。
#
# 坐标约定(实测,用盘上那只 f3 马 (2,5) 校验):row 0 = 第1横线,col 0 = a 列。

_CTRL_PRELUDE = r"""
// 控制器藏在某个 useRef 里,认它的 makeMoveOnRiveBoard 方法
var root = document.getElementById('root');
if (!root) return {err: 'no root'};
var key = Object.keys(root).filter(function (k) { return k.indexOf('__reactContainer') === 0; })[0];
if (!key) return {err: 'no fiber key'};
var ctrl = null, stack = [root[key]], seen = 0;
while (stack.length && seen < 80000 && !ctrl) {
  var f = stack.pop(); seen++;
  if (!f || typeof f !== 'object') continue;
  try {
    var h = f.memoizedState, d = 0;
    while (h && d < 60) {
      var v = h.memoizedState;
      if (v && typeof v === 'object' && 'current' in v) {
        var c = v.current;
        if (c && typeof c === 'object' && typeof c.makeMoveOnRiveBoard === 'function') { ctrl = c; break; }
      }
      h = h.next; d++;
    }
  } catch (e) {}
  if (f.child) stack.push(f.child);
  if (f.sibling) stack.push(f.sibling);
}
if (!ctrl) return {err: 'no controller'};
"""

_NATIVE_MOVE = _CTRL_PRELUDE + r"""
var eng = ctrl.b19_1;                    // initializeGame 里 new Ec(t) 出来的引擎
if (!eng || typeof eng.selectPosition !== 'function') return {err: 'no engine'};
var gs = ctrl.observeGameState().t();
// 起点格上那枚子的 position —— 必须用引擎自己的对象,自造的对不上引用
var pos = null, it = gs.chessBoardState.pieceToPosition.r().g();
while (it.h()) {
  var v = it.i().t();
  if (v.row === P.fromRow && v.col === P.fromCol) { pos = v; break; }
}
if (!pos) return {err: 'from square empty', from: [P.fromRow, P.fromCol]};
eng.selectPosition(pos);                 // 选中 -> 引擎算出该子的合法着法
var lst = ctrl.observeGameState().t().validMovesForSelectedPosition;
var n = lst.j(), mv = null, hits = 0;
for (var i = 0; i < n; i++) {
  var m = lst.p(i);
  if (m.to.row === P.toRow && m.to.col === P.toCol) { hits++; if (!mv) mv = m; }
}
if (!mv) return {err: 'not a legal move here', legalCount: n};
// 同一目标格多个着法 = 升变(选后/车/象/马),别瞎挑
if (hits > 1) return {err: 'ambiguous target (promotion?)', hits: hits};
ctrl.makeMoveOnRiveBoard(mv);
return {ok: true, moveType: (mv.moveType && mv.moveType.constructor && mv.moveType.constructor.name) || '?'};
"""


_PAGE_FEN = _CTRL_PRELUDE + r"""
var gs = ctrl.observeGameState().t();
return {placement: gs.fen.piecePlacementString,
        selected: gs.selectedPosition ? [gs.selectedPosition.row, gs.selectedPosition.col] : null};
"""

_DROP_SELECTION = _CTRL_PRELUDE + r"""
try { ctrl.dropActivePiece(); } catch (e) {}
try { ctrl.b19_1.selectPosition(null); } catch (e) {}
try { ctrl.resetDots(); ctrl.resetHighlights(); } catch (e) {}
return {ok: true};
"""


def page_position() -> dict:
    """页面**引擎**当前的局面(不是服务端)。两边是两个时钟。"""
    return run_js(_PAGE_FEN)


def drop_selection() -> dict:
    """清掉引擎里残留的选中态。失败的一次尝试会把子留在选中态、目标格还亮着圆点。"""
    return run_js(_DROP_SELECTION)


def wait_page_synced(placement: str, timeout: float = 8.0, poll: float = 0.3) -> bool:
    """等页面引擎追上服务端局面。

    ⚠️ 必要性：`wait_for_advance` 盯的是**服务端**，而 `play_move_native` 读的是
    **页面引擎**。服务端可能已经报了奥斯卡的回招，而页面还在播动画没落到本地状态上；
    这时按服务端 FEN 算出来的着法在页面的合法着法表里根本不存在。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if page_position().get("placement") == placement:
                return True
        except Exception:
            pass
        time.sleep(poll)
    return False


def square_to_rowcol(square: str) -> tuple[int, int]:
    """代数格名 -> 引擎的 (row, col)。row 0 = 第1横线,col 0 = a 列。"""
    return int(square[1]) - 1, ord(square[0]) - ord("a")


def play_move_native(uci: str) -> dict:
    """不点任何东西,直接调页面自己的落子入口走一步。"""
    fr, fc = square_to_rowcol(uci[0:2])
    tr, tc = square_to_rowcol(uci[2:4])
    res = run_js(_NATIVE_MOVE, params={"fromRow": fr, "fromCol": fc,
                                       "toRow": tr, "toCol": tc})
    if not res.get("ok"):
        raise RuntimeError(f"原生落子失败: {res}")
    return res


def play_move_by_click(uci: str, player_color: str, pause: float = 0.5) -> dict:
    """用合成点击走一步：先点起点格，再点终点格。

    走的是页面自己的走子逻辑，所以 store / UI / 网络请求全都由页面负责，
    不会出现「服务端走了但页面不知道」的状态分裂。

    ⚠️ `pause` 别再往下调。实测 0.25s 时选中动画还没播完，第二下点击会被整个吃掉 ——
    表现为棋子停在选中态、走子不落（0.25s 下曾连走 19 步只错 1 步，踩在边缘上）。
    0.7s 实测稳；0.5s 是用户要求的折中，靠 play_move_with_retry 兜底。
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
                         settle: float = 0.6, mode: str = "native",
                         fen: str | None = None) -> tuple[dict, int]:
    """走一步，没落下就重试。返回 (最新状态, 实际尝试次数)。

    mode="native"：调页面自己的落子入口，不碰坐标也不碰动画时序 —— 首选。
    mode="click" ：老的合成点击（2026-09-09 起 Rive 棋盘已不理会，保留作历史）。

    给了 `fen` 时，原生模式会先等页面引擎追上这个局面再落子 —— 服务端和页面是
    两个时钟，页面慢一拍时按服务端算出的着法在页面合法表里查不到。
    """
    st = {}
    for attempt in range(1, retries + 1):
        if attempt > 1:
            if mode == "click":
                clear_selection()
            else:
                drop_selection()          # 上一次失败可能把子留在选中态
            time.sleep(1.2)
        else:
            time.sleep(settle)
        try:
            if mode == "native":
                if fen:
                    wait_page_synced(fen.split()[0])
                play_move_native(uci)
            else:
                play_move_by_click(uci, color)
        except (BridgeError, RuntimeError) as exc:
            # 落子这一下失败不该炸掉整局：算作一次尝试，下一轮重来
            print(f"    (第 {attempt} 次落子失败: {exc})")
            continue
        st = wait_for_advance(uid, match_id, prev_len, timeout=18.0)
        if len(st.get("hist", [])) > prev_len or st.get("status") != "active":
            return st, attempt
    return st, retries
