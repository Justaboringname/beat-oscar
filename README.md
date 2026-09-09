# beat-oscar

*[中文说明](README.zh.md)*

Let Stockfish play chess against **Oscar**, Duolingo's in-app chess bot, in Safari.

It reads the position from Duolingo's own API and plays moves by calling the page's own
move entry point — **no screenshots, no image recognition, no board calibration, no
synthetic clicks**.

_Last updated: 2026-09-09 · macOS + Safari only_

## Disclaimer

- The opponent **is Duolingo's built-in AI bot** (Oscar), not a human. This does not
  affect anyone else's games or ratings.
- It uses **undocumented internal endpoints**. Duolingo can break it at any time.
- Automating the client most likely **violates Duolingo's Terms of Service**. Use at
  your own risk — the author is not responsible for anything that happens to your account.
- Published as a study of two things: reverse-engineering a web app's private API, and
  driving Safari from Python without a WebDriver.

## Install

```bash
brew install stockfish
pip install chess
git clone https://github.com/Justaboringname/beat-oscar.git
cd beat-oscar
```

Then enable two things in Safari:

1. **Settings → Advanced → Show features for web developers** (reveals the Develop menu)
2. **Develop → Allow JavaScript from Apple Events**

On first run macOS will ask *"Terminal wants to control Safari"* — allow it (once).

## Usage

Open a game against Oscar in Safari, then:

```bash
python3 bot.py
```

It takes over until the game ends. `Ctrl-C` is safe at any point — every move is
confirmed against the server before continuing, so there is no half-applied state.

| Flag | Meaning |
|---|---|
| `--depth 10` | Stockfish search depth (default 10 — plenty against Oscar, ~0.03s/move) |
| `--movetime 2.0` | Use seconds-per-move instead of depth |
| `--once` | Play a single move |
| `--dry-run` | Compute only, print the engine's suggestion |
| `--api` | PATCH the server directly (board won't move; **untested since 2026-09-09**) |
| `--click` | Fall back to synthetic clicks (**verified broken 2026-09-09**, see below) |
| `--retries 3` | Retries for a move that doesn't land |
| `--engine PATH` | Path to Stockfish (auto-detected; also reads `$STOCKFISH_PATH`) |
| `--threads N` | Engine threads (default: number of performance cores) |
| `--hash MB` | Transposition table size (default 2048) |

On Apple Silicon the thread default counts **performance cores only**
(`hw.perflevel0.physicalcpu`) — including the efficiency cores drags the search down.

### Depth and threads, measured

`--depth` is what costs CPU, and it grows exponentially. On an M1 Pro:

| config | nodes/move | time/move |
|---|---|---|
| depth 10, 1 thread | 5,184 | 0.013s |
| depth 10, 8 threads | 45,538 | 0.030s |
| depth 20, 1 thread | 943,654 | 1.85s |
| depth 20, 8 threads | 13,610,842 | 3.06s |

Stockfish's multithreading is **Lazy SMP**: threads all search the *same* tree and only
communicate through the shared transposition table, so 8 threads do ~9x the *work*, not
1/8th each. That extra work is not wasted, though — a head-to-head at fixed depth 10
(16 games, 8 openings, colours reversed) gave **8 threads 5 wins / 11 draws / 0 losses,
≈ +112 Elo**. Nominal depth being equal does *not* mean equal search quality: the extra
nodes make pruning less aggressive and key lines better verified.

(An earlier version of this README claimed `--depth` made threads pointless. That was
wrong, and the match above is what disproved it.)

None of this matters against Oscar: even 1 thread at depth 10 beats a UCI_Elo-1320
opponent 4–0, and Oscar adapts to the player's rating, which is far below that.

## How it works

```
read position  →  GET /chess/1/<uid>/matches/<matchId>     authoritative FEN
pick move      →  local Stockfish
play move      →  the page's own selectPosition + makeMoveOnRiveBoard
```

Three modules:

- `bridge.py` — the Safari bridge: `osascript` + `do JavaScript`, running synchronous
  JS in the page context
- `duo.py` — protocol layer: read state, native move entry, square→pixel mapping,
  synthetic clicks (dead fallback), retries
- `bot.py` — the main loop

**Why not just call the API?** Calling it directly works (`--api` does exactly that), but
the page's React store never learns about it, so the board **freezes on the old position** —
and any move you then make by hand is sent with a stale position and fights the server. The
move has to go through the page's *own* logic so store, UI and network requests stay
consistent.

**How the page is driven (the default path since 2026-09-09).** The board is rendered with
Rive; none of the 64 squares are DOM elements. A real tap ends up in the controller's
`onRiveEventReceived`, which reads row/col off the Rive event, matches it against
`validMovesForSelectedPosition`, and calls `makeMoveOnRiveBoard`. We join at that step:

```
eng.selectPosition(pos)          # pos comes from the engine's own pieceToPosition —
                                 # a hand-built object won't match by reference
mv = the entry in validMovesForSelectedPosition whose `to` matches
ctrl.makeMoveOnRiveBoard(mv)     # engine move + board animation + the page's own request
```

The controller lives in a `useRef` (found by looking for a `makeMoveOnRiveBoard` method);
the engine is its `b19_1` (the `new Ec(t)` from `initializeGame`). Coordinates are
**row 0 = rank 1, col 0 = the a-file**, verified against a knight on f3. This skips hit
testing, selection animation and click timing entirely, and loses nothing downstream.

## What changed on 2026-09-09

Duolingo moved the board to a **Rive canvas** and added the 「实战练习 / 20:00 / points」
shell. Two consequences:

1. The old board-geometry heuristic (`div` + `width>400` + near-square) can never match —
   the canvas's only container is 748×935 (the extra height is headroom for Oscar's
   character animation to overflow). Worse, the failure surfaced as "the page probably
   isn't in a game", disguising a layout change as a page-state problem. It now takes the
   largest canvas's **parent** rect, and reports the observed canvas/parent sizes on
   mismatch.
2. Synthetic clicks stopped working entirely (see #5 below), so moves now go through the
   page's own entry point.

## The protocol

Observed 2026-08-15 on `duolingo.cn`:

```
PATCH /chess/1/<uid>/matches/<matchId>
  {"fen": <position BEFORE your move>, "move": "b1c3",
   "moveHistory": [...including this move], "fromLanguage": "zh"}
->
  {"match": {"boardFen": <position AFTER Oscar replies>,
             "moveHistory": [...including the reply],
             "playerColor": "white"|"black", "status": "active", ...},
   "opponentMove": "b8c6"}
```

One request = your move + Oscar's reply + the new position. Synchronous request/response,
no WebSocket, no polling.

Terminal values, observed on a real checkmate: `status="completed"`, `outcome="black"`,
`endCondition="checkmate"`. The code stops on `status != "active"` rather than matching
those strings — resignation/timeout/draw values have never been observed.

## Things that cost me time

Every item below is a measured result, not a guess:

1. **Moves are `PATCH`, not `POST`.** Assuming POST because there's a body gets you a 405.
   The server's `Allow: HEAD, OPTIONS, PATCH, GET` answers it directly — ask the server
   before guessing.

2. **`fen` and `moveHistory` are server-authored tokens — echo them back verbatim.**
   Don't regenerate them with python-chess: one differing character in castling rights or
   the halfmove clock desyncs the game silently.

3. **You cannot read the live position from the React fiber.**
   `challenge.match.boardFen` is the *initial* snapshot of the exercise and never updates;
   `challengeStates[].guess.moveHistory` records only your own moves, not the opponent's.
   These hooks are `useSyncExternalStore` (`queue = {value, getSnapshot}`) and
   `queue.dispatch` is `undefined`, so pushing state back would mean reverse-engineering
   the minified store layer. **Get the live position from the server instead.** The fiber
   is only used for invariants like the match id.

4. **Parse the uid strictly from `/chess/1/<uid>/`.** The generic `/users/<id>` paths
   contain *other people's* ids (`friendButtonAvatarUserId`). I grabbed a friend's id once
   before catching it.

5. ~~**Safari's canvas accepts synthetic events** (the iOS app's did not).~~
   **Overturned 2026-09-09**: the Rive board now ignores synthetic events entirely. Ruled
   out: the hit target really is the canvas (`elementFromPoint` returns it), its
   `pointer-events` is `auto`, and splitting down/up across frames changes nothing — not
   even selection fires (`selectedPosition` stays null). **The actual reason is unknown**;
   I stopped digging because the native path makes this moot. `--click` is kept for history
   and does not work.

6. **(Historical)** **You must pause between clicking the origin and the destination square.** This was the
   real cause of failed moves. The symptom is a piece stuck in the selected state (enlarged,
   legal targets dotted) that simply won't move. It looks like "canvas rejects synthetic
   events", but **real mouse events (`cliclick`) fail identically** — it's the selection
   animation eating the second click. 0.25s fails (one game ran 19 moves with a single
   failure — right on the edge), 0.7s is verified stable; the default is now **0.5s**,
   a deliberate compromise that leans on the retry below.
   Retry (`clear_selection()` + re-click) is kept as a backstop and **has been observed
   working** — one game still needed 3 attempts on move 3.

7. **Reloading mid-game loses the game.** The page installs a `beforeunload` guard, and
   after a reload it starts a *new* game rather than restoring the old one. The server
   keeps the match record (GET returns it), but the page never reloads it. So "finish the
   game, then refresh to see the result" does not work.

8. **The AppleScript bridge does not await Promises.** All network calls use synchronous
   `XMLHttpRequest`; both the JS and the AppleScript are passed as temp files to avoid
   quote-escaping hell.

## Known limitations

- **Pawn promotion is untested.** The bot prints a warning and stops rather than clicking
  blindly. Play that move by hand and re-run.
- **Castling works on the native path** — verified 2026-09-09, `O-O` (`e8g8`) played in a
  21-ply game that ended in mate. En passant is still unobserved. Promotion is refused
  outright: several legal moves share the destination square, and matching is by
  destination only.
- **Don't reload the page mid-game** (see #7).
- The native path depends on Duolingo's minified internals (`makeMoveOnRiveBoard`,
  `b19_1`, the row/col convention). A redesign can break it.
- **`--api` has not been re-tested since the 2026-09-09 redesign.** Reading state (GET) is
  verified; the move `PATCH` is not. It is the natural fallback if the native path breaks,
  but treat it as unverified until you land one move with it.
- Board geometry (only used by the dead `--click` path) now takes the largest canvas's
  **parent** rect — the canvas itself is 748×935, not square.
- `--api` desyncs the board on purpose — only use it when you're not watching.
- Verified in Duolingo's *practice* mode (「实战练习」). Rated matches likely use the same
  protocol but that has not been tested.
