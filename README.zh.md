# beat-oscar

*[English](README.md)*

让 Stockfish 替你在 Safari 上跟多邻国的奥斯卡（Oscar）下棋。

读局面走服务端 API，落子靠合成鼠标事件点棋盘 —— **不截屏、不做图像识别、不标定棋盘几何**。

_Last updated: 2026-08-15 · 仅支持 macOS + Safari_

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
| `--depth 20` | Stockfish 搜索深度（默认 20） |
| `--movetime 2.0` | 改成按秒限时，优先于 depth |
| `--once` | 只走一步 |
| `--dry-run` | 只算不走，打印引擎建议 |
| `--api` | 直接 PATCH 服务端（快，但棋盘不会动，见下） |
| `--retries 3` | 单步落子失败的重试次数 |
| `--engine PATH` | 指定 Stockfish 路径（默认自动查找，也认 `$STOCKFISH_PATH`） |
| `--threads N` | 引擎线程数（默认 = 性能核数） |
| `--hash MB` | 置换表大小 MB（默认 2048） |

Apple Silicon 上线程默认**只数性能核**（`hw.perflevel0.physicalcpu`）——把能效核算进去反而拖慢搜索。

关于线程的一个实测提醒：M1 Pro 上 1 → 8 线程，吞吐量涨了约 9.5 倍（39 万 → 372 万 nps），
但固定 3 秒能达到的**名义深度反而少了 1 层**。这是 Lazy SMP 的正常表现——多出来的核用来把搜索
**加宽**而不是加深。用 `--depth` 时多线程只是更快到达同一深度；只有 `--movetime` 才把吞吐转化成
棋力。对付奥斯卡这些都看不出差别。

## 架构

```
读局面  →  GET /chess/1/<uid>/matches/<matchId>     服务端权威 FEN
算招    →  本地 Stockfish
落子    →  合成 pointer/mouse 事件点棋盘             页面自己走子
```

三个文件：

- `bridge.py` — Safari 桥。`osascript` + `do JavaScript`，在页面上下文里跑同步 JS
- `duo.py` — 协议层。读局面、坐标换算、合成点击、重试
- `bot.py` — 主循环

**为什么落子要走合成点击、而不是直接调 API**：直接 PATCH 服务端也能走（`--api` 就是），
但页面的 React store 不知情，棋盘会**停在旧局面不动**，而且你之后在页面上手动走子会拿
旧局面发请求，和服务端打架。合成点击是让页面用它自己的逻辑走子，store / UI / 网络请求
全归页面管，不存在状态分裂。

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

5. **Safari 网页版的 canvas 收合成事件**（老的 app 版不收）。所以不需要 `cliclick`、
   不需要截屏识别棋盘、不需要几何标定——那整条 OS 层路线全部不需要。

6. **点起点和点终点之间必须留够间隔** —— 这是走子失败的真正根因。
   现象是棋子停在选中态（放大、目标格亮着圆点）但就是不落子。曾误判为
   "canvas 拒收合成事件"，但真实鼠标事件（`cliclick`）**同样走不了**，
   所以跟事件真假无关，是选中动画没播完时第二下点击被整个吃掉。
   `play_move_by_click` 的 `pause` 从 0.25s 提到 **0.7s** 后基本消失
   —— 0.25s 下曾连走 19 步只错 1 步，是踩在边缘上，别调回去。

   重试（`clear_selection()` + 重新点击）作为兜底保留，**已实测生效**：
   0.7s 之后仍有一局的第 3 步重试 3 次才落下。

7. **对局中刷新页面会丢局。** 页面有 `beforeunload` 拦截，而且刷新后不会恢复原局，
   而是开一局新的。服务端虽然存着 match 记录（GET 拿得到），但页面不去恢复它。
   所以「下完一局刷新看结果」这个想法是不成立的。

8. **AppleScript 桥不 await Promise。** 所有网络调用一律用同步 `XMLHttpRequest`；
   JS 和 AppleScript 都写成临时文件传递，避开引号转义地狱。

## 已知限制

- **兵升变没实测过。** 遇到升变会打印提示并停下，不瞎点。手动走完那步再重跑。
- **别在下棋途中刷新页面**（见坑 7）。
- 坐标依赖棋盘是页面上唯一那个大正方形容器；Duolingo 改版可能要重新定位。
- `--api` 模式会导致棋盘不同步，只在不看画面时用。
- 是在多邻国的**「实战练习」**模式下验证的。排位对局大概率是同一套协议，但没测过。
