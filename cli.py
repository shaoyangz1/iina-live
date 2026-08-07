"""play-with-mvp 统一命令入口。"""

import sys
import urllib.parse

from live import cli as live_cli
from series import cli as series_cli

_SERIES_MARKERS = ("/bangumi/play/",)
_VALUE_OPTIONS = {
    "--episode",
    "--ep",
    "--grace",
    "--line",
    "--login",
    "--login-refresh",
    "--mode",
    "--player",
    "--port",
    "--quality",
    "--title",
}

_HELP = """usage: cli [live|series] <地址> [选项]

用 IINA/mpv 播放直播或点播内容。

直接传地址时会自动识别 B 站番剧，其余地址按直播处理：
  cli https://www.huya.com/lpl
  cli https://www.bilibili.com/bangumi/play/ss28747 --episode latest

也可显式指定类型（短链接或特殊地址推荐这样用）：
  cli live <直播间地址> [直播选项]
  cli series <番剧地址> [点播选项]

查看分类选项：cli live --help / cli series --help

默认播放器：macOS 使用 IINA，Windows/Linux 使用 mpv；Windows 可先执行 `scoop install mpv`。

B 站登录：cli live --login bilibili；刷新登录凭据：cli live --login-refresh bilibili。

追剧清单：cli series add <番剧地址> / cli series list
"""


def _is_series_url(value: str) -> bool:
    """判断可直接识别的点播地址；短链接可用 series 子命令强制指定。"""
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    return (host == "bilibili.com" or host.endswith(".bilibili.com")) and any(
        marker in path for marker in _SERIES_MARKERS
    )


def _first_positional(argv: list[str]) -> str | None:
    """跳过已知选项及其值，找出实际传给下游的第一个位置参数。"""
    skip_value = False
    for arg in argv:
        if skip_value:
            skip_value = False
            continue
        if arg in _VALUE_OPTIONS:
            skip_value = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return None


def route(argv: list[str]) -> tuple[str, list[str], str]:
    """返回目标类型、转发参数和下游帮助中显示的命令名。"""
    if argv and argv[0] in {"live", "series"}:
        return argv[0], argv[1:], f"cli {argv[0]}"
    url = _first_positional(argv)
    target = "series" if url and _is_series_url(url) else "live"
    return target, argv, "cli"


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args == ["--help"] or args == ["-h"]:
        print(_HELP, end="")
        return 0

    target, forwarded, prog = route(args)
    if target == "series":
        return series_cli.main(forwarded, prog=prog)
    return live_cli.main(forwarded, prog=prog)


if __name__ == "__main__":
    raise SystemExit(main())
