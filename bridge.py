"""Safari <-> Python 桥。

只有一条通道：osascript 的 `do JavaScript`，在 Duolingo 页面上下文里跑同步 JS。
走页面上下文是为了白嫖同源 cookie —— Python 侧不碰任何认证。

两个已知坑（都已绕开）：
  1. AppleScript 桥不 await Promise，async 只会返回 "JavaScript executed"。
     => 所有网络调用一律用同步 XMLHttpRequest。
  2. JS 代码直接嵌进 -e 参数会陷入引号转义地狱。
     => JS 和 AppleScript 都写成临时文件，靠文件传递，零转义。
返回值用 encodeURIComponent 编码成纯 ASCII，规避引号/换行/中文。
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from urllib.parse import unquote


class BridgeError(RuntimeError):
    pass


class NoTabError(BridgeError):
    """Safari 里找不到对局标签页。"""


_APPLESCRIPT = '''
set jsFile to POSIX file "{js_path}"
set jsCode to (read jsFile as «class utf8»)
tell application "Safari"
    repeat with wi from 1 to count of windows
        repeat with ti from 1 to count of tabs of window wi
            set u to URL of tab ti of window wi
            if u contains "{url_match}" then
                return do JavaScript jsCode in tab ti of window wi
            end if
        end repeat
    end repeat
end tell
return "__NO_TAB__"
'''


def run_js(js_body: str, params: dict | None = None, url_match: str = "duolingo") -> object:
    """在对局标签页里执行 js_body，返回解码后的 Python 对象。

    js_body 可以读取 `P`(参数 dict)，并且必须 `return` 一个可 JSON 序列化的值。
    """
    payload = json.dumps(params or {}, ensure_ascii=False)
    wrapped = (
        "(function(){\n"
        f"var P = {payload};\n"
        "var __r = (function(){\n"
        f"{js_body}\n"
        "})();\n"
        "return encodeURIComponent(JSON.stringify(__r === undefined ? null : __r));\n"
        "})()"
    )

    js_path = as_path = None
    try:
        fd, js_path = tempfile.mkstemp(suffix=".js")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(wrapped)

        fd, as_path = tempfile.mkstemp(suffix=".applescript")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_APPLESCRIPT.format(js_path=js_path, url_match=url_match))

        proc = subprocess.run(
            ["osascript", as_path], capture_output=True, text=True, timeout=45
        )
    finally:
        for p in (js_path, as_path):
            if p and os.path.exists(p):
                os.unlink(p)

    if proc.returncode != 0:
        err = proc.stderr.strip()
        # Safari 的「允许 Apple 事件中的 JavaScript」没开时报的就是这个
        if "not allowed" in err or "1743" in err:
            raise BridgeError(
                "Safari 拒绝了 JavaScript。请在 Safari「开发」菜单里勾选\n"
                "  『允许 Apple 事件中的 JavaScript』(Allow JavaScript from Apple Events)"
            )
        raise BridgeError(f"osascript 失败: {err}")

    raw = proc.stdout.strip()
    if raw == "__NO_TAB__":
        raise NoTabError(
            f"Safari 里没有 URL 含 “{url_match}” 的标签页。请先打开多邻国象棋对局页。"
        )
    if not raw:
        raise BridgeError("JS 没有返回值(可能页面正在导航)")

    try:
        return json.loads(unquote(raw))
    except json.JSONDecodeError as exc:
        raise BridgeError(f"返回值解析失败: {exc}\n原始: {raw[:400]}") from exc
