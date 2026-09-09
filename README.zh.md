# beat-oscar

*[English](README.md)*

让 Stockfish 替你在 Safari 上跟多邻国的奥斯卡（Oscar）下棋。

读局面走服务端 API，落子直接调页面自己的落子入口 —— **不截屏、不做图像识别、不标定棋盘几何、
也不合成点击**。

_Last updated: 2026-09-09 · 仅支持 macOS + Safari_

## 免责声明

- 对手**是 Duolingo 内置的 AI 机器人**（奥斯卡），不是真人。这个工具不会影响其他玩家的对局或排名。
- 它用的是 Duolingo **没有公开文档的内部接口**，随时可能因为对方改版而失效。
- 自动化操作很可能**违反 Duolingo 的服务条款**。用不用、后果自负，作者不为账号被限制之类的事负责。
- 仅供学习交流：怎么逆向一个 Web 应用的私有 API、怎么不用 WebDriver 从 Python 驱动 Safari。

## 安装

```bash
brew install stockfish
pip install chess
git clone https://github.com/Justaboringname/beat-oscar.git
cd beat-oscar
```

还需要在 Safari 里打开两个开关：

1. **设置 → 高级 → 显示网页开发者功能**（把「开发」菜单调出来）
2. **开发菜单 → 允许 Apple 事件中的 JavaScript**

第一次运行时 macOS 会弹「"终端" 想要控制 "Safari"」，点允许即可（只弹一次）。

## 用法

Safari 里开一局跟奥斯卡的棋，然后：

```bash
python3 bot.py
```

它会接管到分出胜负。`Ctrl-C` 随时可以停，不会把棋走乱 —— 每步都确认服务端落子了才继续。

| 参数 | 说明 |
|---|---|
| `--depth 10` | Stockfish 搜索深度（默认 10，打奥斯卡绰绰有余，约 0.03 秒/步） |
| `--movetime 2.0` | 改成按秒限时，优先于 depth |
| `--once` | 只走一步 |
| `--dry-run` | 只算不走，打印引擎建议 |
| `--api` | 直接 PATCH 服务端（棋盘不会动；**自 2026-09-09 改版后未重测**） |
| `--click` | 退回老的合成点击落子（**2026-09-09 实测已失效**，见下） |
| `--retries 3` | 单步落子失败的重试次数 |
| `--engine PATH` | 指定 Stockfish 路径（默认自动查找，也认 `$STOCKFISH_PATH`） |
| `--threads N` | 引擎线程数（默认 = 性能核数） |
| `--hash MB` | 置换表大小 MB（默认 2048） |

Apple Silicon 上线程默认**只数性能核**（`hw.perflevel0.physicalcpu`）——把能效核算进去反而拖慢搜索。

### 深度与线程的实测数据

吃 CPU 的是 `--depth`，而且是指数级增长。M1 Pro 上：

| 配置 | 节点/步 | 耗时/步 |
|---|---|---|
| depth 10, 1 线程 | 5,184 | 0.013s |
| depth 10, 8 线程 | 45,538 | 0.030s |
| depth 20, 1 线程 | 943,654 | 1.85s |
| depth 20, 8 线程 | 13,610,842 | 3.06s |

Stockfish 的多线程是 **Lazy SMP**：所有线程搜**同一棵**树，只靠共享置换表通气，所以 8 线程是做了
约 9 倍的**总工作量**，而不是各分 1/8。但那些多出来的工作并非白费——固定 depth 10 的直接对战
（16 局，8 种开局，先后手各半）结果是 **8 线程 5 胜 11 和 0 负，约 +112 Elo**。
名义深度相同 ≠ 搜索质量相同：多搜的节点让剪枝不那么激进、关键变例验证得更充分。

（本文件早先版本称「用 --depth 时多线程只是更快到达同一深度」，那是错的，上面这组对战就是推翻它的证据。）

对打奥斯卡这些都无所谓：单线程 depth 10 就能 4-0 碾压 UCI_Elo 1320 的对手，而奥斯卡是自适应到
玩家水平的，远在那之下。

## 架构

```
读局面  →  GET /chess/1/<uid>/matches/<matchId>     服务端权威 FEN
算招    →  本地 Stockfish
落子    →  合成 pointer/mouse 事件点棋盘             页面自己走子
```

三个文件：

- `bridge.py` — Safari 桥。`osascript` + `do JavaScript`，在页面上下文里跑同步 JS
- `duo.py` — 协议层。读局面、原生落子、坐标换算、合成点击（已失效的回退）、重试
- `bot.py` — 主循环

**为什么落子不直接调 API**：直接 PATCH 服务端也能走（`--api` 就是），但页面的 React store
不知情，棋盘会**停在旧局面不动**，而且你之后在页面上手动走子会拿旧局面发请求，和服务端
打架。所以落子要让页面用**它自己的**逻辑走，store / UI / 网络请求全归页面管，不存在状态分裂。

**怎么让页面自己走（2026-09-09 起的默认路径）**：棋盘是 Rive 画的，64 个格子都不是 DOM
元素。真实点击的归宿是控制器的 `onRiveEventReceived` —— 它从 Rive 事件里取 row/col，去
`validMovesForSelectedPosition` 里匹配出 move 对象，再调 `makeMoveOnRiveBoard`。我们直接
从这一步接进去：

```
eng.selectPosition(pos)          # pos 取自引擎自己的 pieceToPosition，不能自造
mv = validMovesForSelectedPosition 里 to 匹配的那个
ctrl.makeMoveOnRiveBoard(mv)     # 引擎落子 + 棋盘动画 + 页面自己发请求
```

控制器藏在某个 `useRef` 里（认 `makeMoveOnRiveBoard` 方法找它），引擎是它的 `b19_1`
（`initializeGame` 里 `new Ec(t)` 出来的那个）。坐标约定实测为 **row 0 = 第 1 横线、
col 0 = a 列**。这条路把命中测试、选中动画、点击时序全部绕开，下游一个不少。

## 2026-09-09 的改版

多邻国把棋盘换成了 **Rive canvas**，并加了「实战练习 / 20:00 / 点数」这层 UI。两个后果：

1. 老的棋盘几何启发式（`div` + `width>400` + 近方形）**永远匹配不到** —— canvas 唯一的容器
   是 748×935（上方多出来的高度留给奥斯卡角色动画溢出），不是方的。它报的却是
   「页面可能不在对局中」，把版面变更误导成页面状态问题。现在改成取最大 canvas 的**父容器**，
   并在失配时把实测到的 canvas/parent 尺寸打进报错里。
2. 合成点击彻底失效（见下面第 5 条），落子改走页面自己的入口。

## 协议（2026-08-15 实测，duolingo.cn 网页版）

```
PATCH /chess/1/<uid>/matches/<matchId>
  {"fen": <走子前FEN>, "move": "b1c3", "moveHistory": [...含本步], "fromLanguage": "zh"}
->
  {"match": {"boardFen": <奥斯卡回招后的FEN>, "moveHistory": [...含回招],
             "playerColor": "white"|"black", "status": "active", ...},
   "opponentMove": "b8c6"}
```

一次请求 = 走一步 + 收到奥斯卡回招 + 拿到新局面。同步请求-响应，没有 WebSocket。

终局取值（实测过一局将杀）：`status="completed"`、`outcome="black"`、`endCondition="checkmate"`。
代码里一律用 `status != "active"` 判断结束——认输/超时/和棋的取值还没观察到。

## 踩过的坑

按踩到的顺序，每条都是实测结论，不是推测：

1. **走子是 `PATCH` 不是 `POST`。** 从"带 body"想当然当成 POST，收 405。服务端的
   `Allow: HEAD, OPTIONS, PATCH, GET` 直接给了答案——猜之前先问服务器。

2. **`fen` 和 `moveHistory` 是服务端授权的不透明 token，只能原样回传。** 不要用
   python-chess 自己生成：易位权或半步钟差一个字符就会静默失步。

3. **React fiber 里读不到实时局面。** `challenge.match.boardFen` 是关卡初始快照、
   走子中不更新；`challengeStates[].guess.moveHistory` 只记己方着法，不含对手回招。
   这些 hook 是 `useSyncExternalStore`（`queue = {value, getSnapshot}`），
   `queue.dispatch` 是 `undefined`，想 dispatch 回去得逆向混淆过的状态层。
   **实时局面只从服务端 GET 拿。** fiber 只用来取 matchId 这类不变量。

4. **uid 只能从 `/chess/1/<uid>/` 解析。** 通用的 `/users/<id>` 路径里混着好友 uid
   （`friendButtonAvatarUserId`），拿错会打到别人身上。踩过一次，抓出来的是好友的号。

5. ~~**Safari 网页版的 canvas 收合成事件**（老的 app 版不收）。~~
   **2026-09-09 推翻**：现在的 Rive 棋盘**不再理会合成事件**。已排除的解释：命中目标确实
   是 canvas 本身（`elementFromPoint` 返回它）、`pointer-events: auto`、把 down/up 拆到
   不同帧发也没用（连"选中"都不触发，`selectedPosition` 始终为 null）。**真实原因未查明**，
   没有继续查——原生落子已经让这条路多余了。`--click` 作为历史保留，实测走不通。

6. **（历史）点起点和点终点之间必须留够间隔** —— 这是合成点击时代走子失败的真正根因。
   现象是棋子停在选中态（放大、目标格亮着圆点）但就是不落子。曾误判为
   "canvas 拒收合成事件"，但真实鼠标事件（`cliclick`）**同样走不了**，
   所以跟事件真假无关，是选中动画没播完时第二下点击被整个吃掉。
   0.25s 会失败（曾连走 19 步只错 1 步，踩在边缘上），0.7s 实测稳定；
   现在默认取 **0.5s**，是刻意的折中，靠下面的重试兜底。

   重试（`clear_selection()` + 重新点击）作为兜底保留，**已实测生效**：
   0.7s 之后仍有一局的第 3 步重试 3 次才落下。

7. **对局中刷新页面会丢局。** 页面有 `beforeunload` 拦截，而且刷新后不会恢复原局，
   而是开一局新的。服务端虽然存着 match 记录（GET 拿得到），但页面不去恢复它。
   所以「下完一局刷新看结果」这个想法是不成立的。

8. **AppleScript 桥不 await Promise。** 所有网络调用一律用同步 `XMLHttpRequest`；
   JS 和 AppleScript 都写成临时文件传递，避开引号转义地狱。

## 已知限制

- **兵升变没实测过。** 遇到升变会打印提示并停下，不瞎点。手动走完那步再重跑。
- **王车易位在原生路径上已实测可用**（2026-09-09，一局 21 步将杀里走出了 `O-O` / `e8g8`）。
  吃过路兵仍未遇到。升变直接拒绝：多个合法着法共用同一目标格，而匹配只按目标格。
- **别在下棋途中刷新页面**（见坑 7）。
- 原生路径依赖多邻国混淆后的内部结构（`makeMoveOnRiveBoard`、`b19_1`、row/col 约定），
  改版可能打断它。
- **`--api` 自 2026-09-09 改版后没有重测过。** 读局面（GET）验过很多次，但走子的 `PATCH`
  没有。它是原生路径挂掉时最自然的退路，不过在你真用它落成一步之前，按"未验证"看待。
- 棋盘几何（现在只有失效的 `--click` 用）改成取最大 canvas 的**父容器**——canvas 本身
  是 748×935，不是方的。
- `--api` 模式会导致棋盘不同步，只在不看画面时用。
- 是在多邻国的**「实战练习」**模式下验证的。排位对局大概率是同一套协议，但没测过。
