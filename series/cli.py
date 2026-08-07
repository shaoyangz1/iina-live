#!/usr/bin/env python3
"""play-with-mvp 点播命令入口(番剧/影视)。

    uv run cli series <番剧地址> [选项]

点播是完整文件、无断流问题,故只有「直链打开」与「打印地址」两种方式(不像直播有 serve 代理)。

选项:
    --quality Q     清晰度显示名或 qn(如 1080P / 4K / HDR / 112),默认最高
    --line K        选第 K 条线路(0 起),默认 0
    --title T       自定义播放器窗口标题,默认用分集标题
    --episode N     选集:第 N 集(1 起)或 latest(最新一集)
    --player P      播放器:macOS 默认 iina，Windows/Linux 默认 mpv
    --print         只解析打印各清晰度/线路地址,不打开播放器
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile

from live import common

from . import sites

_WATCHLIST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          ".series_watchlist")


def _open_iina(url):
    if sys.platform != "darwin":
        raise RuntimeError("当前平台不支持 IINA，请使用 --player mpv")
    subprocess.run(["open", url], check=False)


def _open_iina_m3u(rid, title, url, headers=None, audio=None):
    """本地 m3u 让 IINA 靠 #EXTINF 名显示标题;带平台 referer/UA + DASH 音轨。"""
    d = os.path.join(tempfile.gettempdir(), "MPV-SERIES")
    os.makedirs(d, exist_ok=True)
    m3u = os.path.join(d, f"{rid}.m3u")
    with open(m3u, "w", encoding="utf-8") as f:
        f.write(common.single_m3u(title, url))
    _open_iina(common.iina_local_url(title, m3u, headers, audio, reconnect=False))


def _open_mpv(url, title, headers, audio=None):
    args = [common.mpv_executable(), url, f"--force-media-title={title}", "--ytdl=no",
            "--audio=auto", "--mute=no"]
    if sys.platform == "win32":
        args.append("--audio-device=auto")
    if audio:                                        # DASH:独立音轨
        args.append(f"--audio-file={audio}")
    if headers.get("Referer"):
        args.append(f"--referrer={headers['Referer']}")
    if headers.get("User-Agent"):
        args.append(f"--user-agent={headers['User-Agent']}")
    subprocess.run(args, check=False)


def _episode_arg(s):
    """--episode 取值:正整数(第几集,1 起)或 'latest'(最新一集)。"""
    if s == "latest":
        return "latest"
    try:
        n = int(s)
    except ValueError:
        raise argparse.ArgumentTypeError("需为正整数或 latest")
    if n < 1:
        raise argparse.ArgumentTypeError("需 >= 1")
    return n


def main(argv: list[str] | None = None, *, prog: str = "mpv-series") -> int:
    av = argv or []
    # 子命令: add / list
    if av and av[0] in {"add", "list"}:
        return _watchlist_cmd(av, prog)

    ap = argparse.ArgumentParser(prog=prog)
    ap.add_argument("url", help="番剧/影视地址,如 https://www.bilibili.com/bangumi/play/ss28747 或 ep123")
    ap.add_argument("--quality", default=None)
    ap.add_argument("--line", type=int, default=0)
    ap.add_argument("--title", default=None)
    ap.add_argument("--episode", "--ep", type=_episode_arg, default=None, metavar="N|latest",
                    help="选集:第 N 集(1 起)或 latest(最新一集)")
    ap.add_argument("--player", default=common.default_player(), choices=["iina", "mpv"])
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="只解析打印各清晰度/线路地址,不打开播放器")
    a = ap.parse_args(argv)
    try:
        return play(a.url, a)
    except KeyboardInterrupt:
        print("\n已退出")
        return 0


def _load_watchlist():
    if not os.path.exists(_WATCHLIST):
        return []
    try:
        with open(_WATCHLIST, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return []


def _save_watchlist(items):
    with open(_WATCHLIST, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _watchlist_cmd(argv, prog):
    if argv[0] == "list":
        items = _load_watchlist()
        if not items:
            print("追剧列表为空。用 {0} add <番剧地址> 添加。".format(prog))
            return 0
        for i, it in enumerate(items, 1):
            print(f"  {i}. {it['nick']}（共 {it['total']} 集）\n     {it['url']}")
        return 0
    # add
    if len(argv) < 2:
        print("用法: {0} add <番剧地址>".format(prog))
        return 1
    url = argv[1]
    items = _load_watchlist()
    if any(it["url"] == url for it in items):
        print("已在追剧列表中。")
        return 0
    try:
        info = sites.get_season_info(url)
    except Exception as e:
        print(f"无法解析番剧: {e}")
        return 1
    eps = info["episodes"]
    total = max((int(e["title"]) for e in eps if e["title"].isdigit()), default=len(eps))
    items.append({"url": url, "nick": info["nick"], "total": total})
    _save_watchlist(items)
    print(f"已添加: {info['nick']}（共 {total} 集）")
    return 0


def _resolve(url, a, total=None):
    """解析一集,返回 (headers, url, title, audio) 或全是 None。"""
    info = sites.parse(url, episode=a.episode)
    headers = sites.play_headers(url)
    nick = info["nick"]
    if total and total > 1:
        nick += f"（共 {total} 集，当前第 {a.episode} 集）"
    print(f"番名 : {nick}")
    print(f"标题 : {info['title']}")
    if not info["living"]:
        print("未取到可播放内容(可能未上线或地区限制)。")
        return None, None, None, None

    name, stream = common.pick(info, a.quality)
    if stream is None:
        print("未取到可播放的流。")
        return None, None, None, None
    title = a.title or info.get("title") or info.get("nick")
    urls = [stream["url"]] + stream["backups"]
    url_pick = urls[a.line % len(urls)]
    audio = stream.get("audio")
    print(f"清晰度 : {name}")

    if a.print_only:
        for n, s in sorted(info["streams"].items(), key=lambda x: -x[1]["quality"]):
            print(f"\n[{n}] quality={s['quality']} 线路数={1 + len(s['backups'])}")
            for i, x in enumerate([s["url"]] + s["backups"]):
                print(f"  线路{i}: {x}")
            if s.get("audio"):
                print(f"  音轨 : {s['audio']}")
        return None, None, None, None

    return headers, url_pick, title, audio


def play(url, a):
    # 取分集列表
    season = sites.get_season_info(url)
    eps = season["episodes"]
    total = max(
        (int(e["title"]) for e in eps if e["title"].isdigit()),
        default=len(eps))

    # 未指定集数时:交互选择,回车默认最新
    interactive = False
    if not a.print_only:
        if a.episode is None:
            interactive = True
            if len(eps) > 1:
                print(f"番名 : {season['nick']}（共 {total} 集）")
                print(f"输入集数 1-{total},回车默认最新")
                try:
                    choice = input().strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n已退出")
                    return 0
                if choice:
                    a.episode = int(choice)
                else:
                    a.episode = total
        elif a.episode == "latest":
            a.episode = total

    while True:
        headers, url_pick, title, audio = _resolve(url, a, total)
        if headers is None:
            return 1 if not a.print_only else 0
        if a.print_only:
            return 0

        info = sites.parse(url, episode=a.episode)
        if a.player == "mpv":
            _open_mpv(url_pick, title, headers, audio)
        else:
            _open_iina_m3u(info["rid"], title, url_pick, headers, audio)

        if not interactive:
            return 0

        # 找下一集,没有则退出
        nxt = a.episode + 1
        hits = [e for e in eps if str(e.get("title", "")).strip() == str(nxt)]
        if not hits:
            return 0

        print(f"\n继续播放下一集? [Y/n]", end="", flush=True)
        try:
            ans = input().strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出")
            return 0
        if ans and ans != "y":
            return 0
        a.episode = nxt


if __name__ == "__main__":
    raise SystemExit(main())
