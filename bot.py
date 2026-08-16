#!/usr/bin/env python3
"""多邻国象棋 · Safari 自动对弈。

  读局面 —— 向服务端 GET 权威 FEN
  算招   —— 本地 Stockfish
  落子   —— 合成鼠标事件点棋盘，让页面用它自己的逻辑走子

落子走页面自己的逻辑，所以 store / UI / 网络请求都归页面管，棋盘会实时动，
不会出现「服务端走了但页面停在旧局面」的状态分裂。

用法：
    python3 bot.py                 # 自动下完整局
    python3 bot.py --once          # 只走一步
    python3 bot.py --dry-run       # 只算不走
    python3 bot.py --api           # 直接 PATCH 服务端(快，但棋盘不会动)

已知限制：
  * 兵升变没实测过 —— 会提示并停下，不瞎点
  * 刷新页面会丢掉当前 bot 对局，别在下棋途中刷新
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time

import chess
import chess.engine

import duo
from bridge import BridgeError, NoTabError

_STOCKFISH_CANDIDATES = (
    "/opt/homebrew/bin/stockfish",      # Apple Silicon Homebrew
    "/usr/local/bin/stockfish",         # Intel Homebrew
    "/opt/local/bin/stockfish",         # MacPorts
)


def default_threads() -> int:
    """默认用性能核的数量。

    在 Apple Silicon 上别把能效核也算进去 —— 它们比性能核慢得多，
    搜索是同步的，慢核会拖住整体。拿不到就退回 os.cpu_count()-2。
    """
    try:
        out = subprocess.run(["sysctl", "-n", "hw.perflevel0.physicalcpu"],
                             capture_output=True, text=True, timeout=3)
        n = int(out.stdout.strip())
        if n > 0:
            return n
    except Exception:
        pass
    return max(1, (os.cpu_count() or 2) - 2)


def find_stockfish(explicit: str | None = None) -> str:
    """定位 Stockfish：--engine > $STOCKFISH_PATH > PATH > 常见安装位置。"""
    for cand in (explicit, os.environ.get("STOCKFISH_PATH")):
        if cand:
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
            raise RuntimeError(f"指定的 Stockfish 不可用: {cand}")
    found = shutil.which("stockfish")
    if found:
        return found
    for cand in _STOCKFISH_CANDIDATES:
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    raise RuntimeError(
        "找不到 Stockfish。装一个：brew install stockfish\n"
        "或者用 --engine /path/to/stockfish 指定，也可以设 STOCKFISH_PATH 环境变量。"
    )


def my_turn(fen: str, color: str) -> bool:
    return fen.split()[1] == ("w" if color == "white" else "b")


def wait_ready(timeout: float = 25.0, poll: float = 1.0):
    """等页面就绪。

    刚刷新完那几秒 uid 是拿不到的 —— uid 从已发请求的 URL 里解析，而
    performance 记录在导航时被清空，页面得先把 /chess/1/<uid>/matches 发出去。
    fiber 在渲染途中同样可能读不到。所以轮询而不是一次定生死。
    """
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            uid = duo.get_uid()
            return uid, duo.server_state(uid, duo.get_match_id())
        except Exception as exc:            # 页面还没准备好，等
            last = exc
            time.sleep(poll)
    raise last or RuntimeError("页面迟迟没就绪")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--depth", type=int, default=20, help="Stockfish 搜索深度")
    ap.add_argument("--movetime", type=float, default=None, help="每步秒数(给了则优先于 depth)")
    ap.add_argument("--delay", type=float, default=0.8, help="每步之间的间隔秒数")
    ap.add_argument("--once", action="store_true", help="只走一步")
    ap.add_argument("--dry-run", action="store_true", help="只算不走")
    ap.add_argument("--api", action="store_true", help="直接 PATCH 服务端(棋盘不会动)")
    ap.add_argument("--retries", type=int, default=3, help="单步落子失败的重试次数")
    ap.add_argument("--engine", default=None, help="Stockfish 可执行文件路径(默认自动查找)")
    ap.add_argument("--threads", type=int, default=None,
                    help="引擎线程数(默认=性能核数)")
    ap.add_argument("--hash", type=int, default=2048,
                    help="置换表 MB(默认 2048)")
    args = ap.parse_args()

    try:
        uid, st = wait_ready()
    except (NoTabError, BridgeError, RuntimeError) as exc:
        print(f"✗ {exc}")
        return 2

    match_id = st["id"]
    color = st.get("color") or "white"
    print(f"✓ 对局 {match_id}")
    print(f"  执子: {'白' if color == 'white' else '黑'} | uid {uid} | "
          f"模式: {'API 直连' if args.api else '合成点击'}")
    print(f"  当前: {len(st['hist'])} 步  {st['fen']}")

    try:
        engine_path = find_stockfish(args.engine)
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 2
    engine = chess.engine.SimpleEngine.popen_uci(engine_path)
    threads = args.threads if args.threads else default_threads()
    try:
        engine.configure({"Threads": threads, "Hash": args.hash})
        print(f"  引擎: Stockfish @ {threads} 线程 / {args.hash}MB 置换表")
    except chess.engine.EngineError as exc:
        print(f"  ⚠ 引擎参数设置失败，回落到默认值: {exc}")
    limit = (chess.engine.Limit(time=args.movetime) if args.movetime
             else chess.engine.Limit(depth=args.depth))

    plies = 0
    try:
        while True:
            if st.get("status") != "active":
                print(f"\n■ 对局结束  status={st.get('status')}  "
                      f"outcome={st.get('outcome')}  endCondition={st.get('endCondition')}")
                break

            fen, hist = st["fen"], st["hist"]
            board = chess.Board(fen)

            if board.is_game_over():
                print(f"\n■ 终局: {board.result()} ({board.outcome().termination.name})")
                break

            if not my_turn(fen, color):
                time.sleep(1.0)
                st = duo.server_state(uid, match_id)
                continue

            info = engine.analyse(board, limit)
            best = info["pv"][0]
            score = info["score"].white() if color == "white" else info["score"].black()
            print(f"\n[{plies + 1}] {board.san(best)}  ({best.uci()})  评估 {score}")

            if best.promotion is not None:
                print("  ⚠ 兵升变，升变 UI 没实测过，停下来免得瞎点。请手动走这步后重跑。")
                break
            if args.dry_run:
                print("  --dry-run，不走")
                break

            before = len(hist)
            if args.api:
                resp = duo.submit_move(uid, match_id, fen, best.uci(), hist)
                m = resp["match"]
                st = {"id": m["id"], "fen": m["boardFen"], "hist": m.get("moveHistory", []),
                      "color": m.get("playerColor", color), "status": m.get("status"),
                      "outcome": m.get("outcome"), "endCondition": m.get("endCondition")}
            else:
                st, tries = duo.play_move_with_retry(
                    best.uci(), color, uid, match_id, before, retries=args.retries)
                if tries > 1:
                    print(f"    (重试 {tries} 次才落下)")

            new_hist = st.get("hist", [])
            if len(new_hist) <= before:
                print(f"  ⚠ 重试 {args.retries} 次仍未落下，停机以免走乱。")
                print(f"     期望 >{before} 步，实际 {len(new_hist)} 步")
                break
            plies += 1

            if len(new_hist) > before + 1:
                opp = new_hist[before + 1]
                pre = chess.Board(fen)
                pre.push(best)
                try:
                    print(f"    奥斯卡 {pre.san(chess.Move.from_uci(opp))}  ({opp})")
                except Exception:
                    print(f"    奥斯卡 {opp}")

            if args.once:
                print("\n--once，收工")
                break
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print("\n(中断)")
    finally:
        engine.quit()

    print(f"\n共走 {plies} 步")
    return 0


if __name__ == "__main__":
    sys.exit(main())
