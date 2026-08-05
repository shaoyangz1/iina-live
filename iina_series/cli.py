#!/usr/bin/env python3
"""iina-series 命令行入口(点播:番剧/影视)。

    python -m iina_series <番剧地址> [选项]

点播是完整文件、无断流问题,故只有「直链打开」与「打印地址」两种方式(不像直播有 serve 代理)。

选项:
    --quality Q     清晰度显示名或 qn(如 1080P / 4K / HDR / 112),默认最高
    --line K        选第 K 条线路(0 起),默认 0
    --title T       自定义标题,默认用分集标题
    --episode N     选集:第 N 集(1 起)或 latest(最新一集)
    --player P      iina(默认) / mpv
    --print         只解析打印各清晰度/线路地址,不打开播放器
"""
import argparse
import os
import subprocess
import tempfile

from iina_live import common
from . import sites


def _open_iina(url):
    subprocess.run(["open", url])


def _open_iina_m3u(rid, title, url, headers=None, audio=None):
    """本地 m3u 让 IINA 靠 #EXTINF 名显示标题;带平台 referer/UA + DASH 音轨。"""
    d = os.path.join(tempfile.gettempdir(), "IINA-SERIES")
    os.makedirs(d, exist_ok=True)
    m3u = os.path.join(d, f"{rid}.m3u")
    with open(m3u, "w") as f:
        f.write(common.single_m3u(title, url))
    _open_iina(common.iina_local_url(title, m3u, headers, audio))


def _open_mpv(url, title, headers, audio=None):
    args = ["mpv", url, f"--force-media-title={title}", "--ytdl=no",
            f"--stream-lavf-o={common.RECONNECT}"]
    if audio:                                        # DASH:独立音轨
        args.append(f"--audio-file={audio}")
    if headers.get("Referer"):
        args.append(f"--referrer={headers['Referer']}")
    if headers.get("User-Agent"):
        args.append(f"--user-agent={headers['User-Agent']}")
    subprocess.Popen(args)


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


def main():
    ap = argparse.ArgumentParser(prog="iina-series")
    ap.add_argument("url", help="番剧/影视地址,如 https://www.bilibili.com/bangumi/play/ss28747 或 ep123")
    ap.add_argument("--quality", default=None)
    ap.add_argument("--line", type=int, default=0)
    ap.add_argument("--title", default=None)
    ap.add_argument("--episode", "--ep", type=_episode_arg, default=None, metavar="N|latest",
                    help="选集:第 N 集(1 起)或 latest(最新一集)")
    ap.add_argument("--player", default="iina", choices=["iina", "mpv"])
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="只解析打印各清晰度/线路地址,不打开播放器")
    a = ap.parse_args()
    return play(a.url, a)


def play(url, a):
    info = sites.parse(url, episode=a.episode)
    headers = sites.play_headers(url)
    total = info.get("episodes")
    if info.get("season_id"):
        line = f"整季 : ss{info['season_id']}"
        if total and total > 1:
            cur = total if a.episode == "latest" else (a.episode or 1)
            tag = "最新" if a.episode == "latest" else ""
            line += f"（共 {total} 集，当前第 {cur} 集{tag}，--episode N|latest 选集）"
        print(line)
    print(f"编号 : {info['rid']}")
    print(f"番名 : {info['nick']}")
    print(f"标题 : {info['title']}")
    if not info["living"]:
        print("未取到可播放内容(可能未上线或地区限制)。")
        return 1

    name, stream = common.pick(info, a.quality)
    if stream is None:
        print("未取到可播放的流。")
        return 1
    title = a.title or info.get("title") or info.get("nick")
    urls = [stream["url"]] + stream["backups"]
    url_pick = urls[a.line % len(urls)]
    audio = stream.get("audio")
    print(f"清晰度 : {name} (quality={stream['quality']}, 线路数={len(urls)})")

    if a.print_only:
        for n, s in sorted(info["streams"].items(), key=lambda x: -x[1]["quality"]):
            print(f"\n[{n}] quality={s['quality']} 线路数={1 + len(s['backups'])}")
            for i, x in enumerate([s["url"]] + s["backups"]):
                print(f"  线路{i}: {x}")
            if s.get("audio"):
                print(f"  音轨 : {s['audio']}")
        return 0

    if a.player == "mpv":
        _open_mpv(url_pick, title, headers, audio)
    else:
        _open_iina_m3u(info["rid"], title, url_pick, headers, audio)
    note = "，已配 DASH 音轨" if audio else ""
    print(f"已用直链打开 ({a.player}){note}。点播为完整文件,卡住重开即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
