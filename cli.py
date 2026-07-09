#!/usr/bin/env python3
"""iina-live 命令行入口。

    python cli.py <房间地址> [选项]

选项:
    --quality Q     清晰度显示名或码率(如 "原画" / 蓝光10M / 2000)，默认最高
    --line K        直链/m3u 模式下选第 K 条线路(0 起)，默认 0
    --title T       自定义 IINA 窗口标题，默认用房间名(主播名)
    --mode MODE     打开方式，默认 serve:
                      serve  本地转流代理(推荐)：固定地址，自动跨 2 分钟断流自愈
                      m3u    多线路播放列表：卡住时在 IINA 播放列表切备用线路
                      direct 单条 flv 直链：最简单，卡住无法恢复
                      print  只解析打印各清晰度地址，不打开播放器
    --port P        serve 模式端口，默认 8787
    --player P      direct/m3u 模式播放器: iina(默认) / mpv
"""
import argparse
import os
import subprocess
import sys
import tempfile
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sites
import common


def _open_iina(url):
    subprocess.run(["open", url])


def _open_mpv(flv, title, headers):
    args = ["mpv", flv, f"--force-media-title={title}", "--ytdl=no",
            f"--stream-lavf-o={common.RECONNECT}"]
    if headers.get("Referer"):
        args.append(f"--referrer={headers['Referer']}")
    if headers.get("User-Agent"):
        args.append(f"--user-agent={headers['User-Agent']}")
    subprocess.Popen(args)


def main():
    ap = argparse.ArgumentParser(prog="iina-live")
    ap.add_argument("url", help="直播间地址，如 https://www.huya.com/lpl")
    ap.add_argument("--quality", default=None)
    ap.add_argument("--line", type=int, default=0)
    ap.add_argument("--title", default=None)
    ap.add_argument("--mode", default="serve", choices=["serve", "m3u", "direct", "print"])
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--player", default="iina", choices=["iina", "mpv"])
    ap.add_argument("--grace", type=int, default=180,
                    help="serve 模式:无连接空闲多少秒后自动退出，<=0 常驻，默认 180")
    a = ap.parse_args()

    info = sites.parse(a.url)
    headers = sites.play_headers(a.url)
    print(f"房间号 : {info['rid']}")
    print(f"主播   : {info['nick']}")
    print(f"标题   : {info['title']}")
    print(f"直播中 : {info['living']}")
    if not info["living"]:
        print("主播未开播。")
        return 1

    name, stream = common.pick(info, a.quality)
    title = a.title or info["nick"] or info["title"]   # 默认用房间名(主播名)
    urls = [stream["url"]] + stream["backups"]
    flv = urls[a.line % len(urls)]
    print(f"清晰度 : {name} (quality={stream['quality']}, 线路数={len(urls)})")

    if a.mode == "print":
        for n, s in sorted(info["streams"].items(), key=lambda x: -x[1]["quality"]):
            print(f"\n[{n}] quality={s['quality']} 线路数={1 + len(s['backups'])}")
            for i, u in enumerate([s["url"]] + s["backups"]):
                print(f"  线路{i}: {u}")
        return 0

    if a.mode == "direct":
        if a.player == "mpv":
            _open_mpv(flv, title, headers)
        else:
            _open_iina(common.iina_url(title, flv, headers))
        print(f"已用直链打开 ({a.player})。注意:卡住无法自动恢复。")
        return 0

    if a.mode == "m3u":
        d = os.path.join(tempfile.gettempdir(), "IINA-LIVE")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{info['rid']}.m3u")
        with open(path, "w") as f:
            f.write(common.m3u_content(title, stream))
        # m3u 里是 flv 直链，mpv 抓取时仍需 referer/UA，故用带头的 iina_url
        if a.player == "mpv":
            _open_mpv(path, title, headers)
        else:
            _open_iina(common.iina_url(title, path, headers))
        print(f"已用 m3u 播放列表打开:{path}\n卡住时在 IINA 播放列表切换「备用N」。")
        return 0

    # serve 模式：启动本地代理（阻塞），并唤起播放器
    # 路径用房间名，服务器会据此自动解析（/lpl.flv → 同平台/lpl）
    slug = urllib.parse.urlparse(a.url).path.strip("/").split("/")[0] or "live"
    local = f"http://127.0.0.1:{a.port}/{slug}.flv"
    here = os.path.dirname(os.path.abspath(__file__))
    srv = subprocess.Popen([sys.executable, os.path.join(here, "server.py"),
                            a.url, str(a.port), a.quality or "", str(a.grace)])
    import time
    time.sleep(4)
    if a.player == "mpv":
        _open_mpv(local, title, {})   # 本地代理已带好平台头，mpv 直连 localhost 不需要
    else:
        # IINA 标题栏对网络直链只显示文件名，force-media-title 不生效；
        # 改用单条本地 m3u，靠 #EXTINF 名让 IINA 显示房间名（iina-plus 同款机制）。
        d = os.path.join(tempfile.gettempdir(), "IINA-LIVE")
        os.makedirs(d, exist_ok=True)
        m3u = os.path.join(d, f"{info['rid']}.m3u")
        with open(m3u, "w") as f:
            f.write(common.single_m3u(title, local))
        _open_iina(common.iina_local_url(title, m3u))
    print(f"本地代理已启动 (PID {srv.pid})，固定地址:{local}")
    print("直播每 ~2 分钟断流由服务器自动重解析续播，播放器无感。Ctrl+C 结束。")
    try:
        srv.wait()
    except KeyboardInterrupt:
        srv.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
