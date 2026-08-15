# beat-oscar

*[中文说明](README.zh.md)*

Let Stockfish play chess against **Oscar**, Duolingo's in-app chess bot, in Safari.

It reads the position from Duolingo's own API and plays moves by synthesizing mouse
events on the board — **no screenshots, no image recognition, no board calibration**.

_Last updated: 2026-08-15 · macOS + Safari only_

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
| `--depth 20` | Stockfish search depth (default 20) |
| `--movetime 2.0` | Use seconds-per-move instead of depth |
| `--once` | Play a single move |
| `--dry-run` | Compute only, print the engine's suggestion |
| `--api` | PATCH the server directly (faster, but the board won't move — see below) |
| `--retries 3` | Retries for a move that doesn't land |
| `--engine PATH` | Path to Stockfish (auto-detected; also reads `$STOCKFISH_PATH`) |

## How it works

```
read position  →  GET /chess/1/<uid>/matches/<matchId>     authoritative FEN
pick move      →  local Stockfish
play move      →  synthesized pointer/mouse events on the board
```

Three modules:

- `bridge.py` — the Safari bridge: `osascript` + `do JavaScript`, running synchronous
  JS in the page context
- `duo.py` — protocol layer: read state, square→pixel mapping, synthetic clicks, retries
- `bot.py` — the main loop

**Why click the board instead of just calling the API?** Calling it directly works
(`--api` does exactly that), but the page's React store never learns about it, so the
board **freezes on the old position** — and any move you then make by hand is sent with
a stale position and fights the server. Clicking makes the page play the move through
its own logic, so store, UI and network requests all stay consistent.

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

5. **Safari's canvas accepts synthetic events** (the iOS app's did not). That kills the
   entire OS-level approach — no `cliclick`, no screen capture, no geometry calibration.

6. **You must pause between clicking the origin and the destination square.** This was the
   real cause of failed moves. The symptom is a piece stuck in the selected state (enlarged,
   legal targets dotted) that simply won't move. It looks like "canvas rejects synthetic
   events", but **real mouse events (`cliclick`) fail identically** — it's the selection
   animation eating the second click. Raising `pause` from 0.25s to **0.7s** fixed it;
   at 0.25s one game ran 19 moves with a single failure, i.e. right on the edge.
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
- **Don't reload the page mid-game** (see #7).
- Square coordinates assume the board is the only large square container on the page; a
  Duolingo redesign may require re-locating it.
- `--api` desyncs the board on purpose — only use it when you're not watching.
- Verified in Duolingo's *practice* mode (「实战练习」). Rated matches likely use the same
  protocol but that has not been tested.
