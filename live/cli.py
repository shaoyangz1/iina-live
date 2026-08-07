#!/usr/bin/env python3
"""play-with-mvp 直播命令入口。

    uv run cli <房间地址> [选项]

选项:
    --quality Q     清晰度显示名或码率(如 "原画" / 蓝光10M / 2000)，默认最高
    --line K        直链/m3u 模式下选第 K 条线路(0 起)，默认 0
    --title T       自定义播放器窗口标题，默认用房间名(主播名)
    --mode MODE     打开方式，默认 serve:
                      serve       本地转流代理(推荐)：固定地址，自动跨 2 分钟断流自愈
                      serve-only  只起常驻代理、不打开播放器(房间可省)：别处用 serve 复用它播放，日志集中于此
                      m3u         多线路播放列表：卡住时切备用线路
                      direct      单条 flv 直链：最简单，卡住无法恢复
                      print       只解析打印各清晰度地址，不打开播放器
    --port P        serve 模式端口，默认 8787
    --player P      播放器:macOS 默认 iina，Windows/Linux 默认 mpv
    --login bilibili 扫码登录(终端出二维码)，cookie 存本地供 B 站取流解锁原画/4K
    --login-refresh bilibili 刷新 B 站登录凭据(需先用新版扫码登录保存 refresh_token)
    --login-status  查看 B 站登录状态、会员类型与 cookie 剩余有效期

番剧/影视点播可直接传番剧地址，或用:uv run cli series <番剧地址>
"""
import argparse
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from . import common, sites

PORT_SCAN = 20   # 从 --port 起最多向后扫描多少个端口
PROXY_MARKERS = (b"play-with-mvp", b"iina-live")


def _probe(port):
    """探测端口:'ours'=本 skill 代理 / 'free'=无人监听 / 'other'=被别的占用。"""
    try:
        # 超时给到 4s:某些环境对已关闭 loopback 端口要 ~2s 才回“拒绝”,超时太短会在收到
        # 拒绝前先超时、把空闲端口误判为 other。正常机器瞬间拒绝,上限无副作用。
        r = urllib.request.urlopen(f"http://127.0.0.1:{port}/__ping__", timeout=4)
        body = r.read(32)
        return "ours" if any(marker in body for marker in PROXY_MARKERS) else "other"
    except urllib.error.URLError as e:
        return "free" if isinstance(e.reason, ConnectionRefusedError) else "other"
    except Exception:
        return "other"


def _wait_ready(port, timeout=10.0):
    """轮询 __ping__ 直到本 skill 的代理就绪或超时,返回是否就绪。

    替代启动后固定 sleep:解析快时立刻返回(不空等),解析慢时也不会过早打开播放器。"""
    deadline = time.monotonic() + timeout
    while True:
        if _probe(port) == "ours":
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.15)


def _choose_port(base):
    """优先复用已有代理端口；否则返回第一个空闲端口。返回 (port, reuse)。

    并发探测:某些环境对已关闭的 loopback 端口也要 ~2s 才返回“拒绝”,串行扫 20 个端口
    会拖到 ~40s,故并发一次扫完。"""
    ports = list(range(base, base + PORT_SCAN))
    with ThreadPoolExecutor(max_workers=PORT_SCAN) as ex:
        res = dict(zip(ports, ex.map(_probe, ports)))
    for p in ports:        # 先找可复用的
        if res[p] == "ours":
            return p, True
    for p in ports:        # 再找空闲的新起
        if res[p] == "free":
            return p, False
    raise RuntimeError(f"{base}~{base + PORT_SCAN - 1} 端口都被占用，换个 --port")


def _serve_url(port, room, quality):
    """本地代理地址:把完整房间地址与清晰度写进 query。

    这样无论是新起的代理还是复用别处已在跑的代理,server 都按本次请求携带的 room/quality
    解析——复用时 --quality 不再被忽略,也不受被复用代理启动平台的限制。"""
    q = {"room": room}
    if quality:
        q["quality"] = quality
    # safe="/:"：放行 : 与 / 不做百分号编码,常见地址保持可读;& ? # 等仍会编码,不破坏 query 结构。
    return f"http://127.0.0.1:{port}/live.flv?" + urllib.parse.urlencode(q, safe="/:")


def _open_iina(url):
    if sys.platform != "darwin":
        raise RuntimeError("当前平台不支持 IINA，请使用 --player mpv")
    subprocess.run(["open", url], check=False)


def _open_iina_m3u(rid, title, url, headers=None):
    """给 IINA 一个含直链的本地 m3u,靠 #EXTINF 名显示标题(IINA 对网络直链 force-media-title
    在标题栏不生效);direct 模式带 referer/UA,serve 模式 headers 留空(代理已带头)。"""
    d = os.path.join(tempfile.gettempdir(), "MPV-LIVE")
    os.makedirs(d, exist_ok=True)
    m3u = os.path.join(d, f"{rid}.m3u")
    with open(m3u, "w", encoding="utf-8") as f:
        f.write(common.single_m3u(title, url))
    _open_iina(common.iina_local_url(title, m3u, headers))


def _open_mpv(flv, title, headers):
    args = [common.mpv_executable(), flv, f"--force-media-title={title}", "--ytdl=no",
            f"--stream-lavf-o={common.RECONNECT}"]
    if headers.get("Referer"):
        args.append(f"--referrer={headers['Referer']}")
    if headers.get("User-Agent"):
        args.append(f"--user-agent={headers['User-Agent']}")
    subprocess.Popen(args)


def main(argv: list[str] | None = None, *, prog: str = "mpv-live") -> int:
    ap = argparse.ArgumentParser(prog=prog)
    ap.add_argument("url", nargs="?", default=None,
                    help="直播间地址，如 https://www.huya.com/lpl；--mode serve-only 可省")
    ap.add_argument("--quality", default=None)
    ap.add_argument("--line", type=int, default=0)
    ap.add_argument("--title", default=None)
    ap.add_argument("--mode", default="serve",
                    choices=["serve", "serve-only", "m3u", "direct", "print"])
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--player", default=common.default_player(), choices=["iina", "mpv"])
    ap.add_argument("--grace", type=int, default=180,
                    help="serve 模式:无连接空闲多少秒后自动退出，<=0 常驻，默认 180")
    ap.add_argument("--login", choices=["bilibili"], default=None,
                    help="扫码登录(目前支持 bilibili):终端出二维码，登录后 cookie 存本地供取流解锁原画/4K")
    ap.add_argument("--login-refresh", choices=["bilibili"], default=None,
                    help="刷新 B 站登录凭据(需已有 refresh_token)")
    ap.add_argument("--login-status", action="store_true",
                    help="查看 B 站登录状态、会员类型与 cookie 剩余有效期")
    a = ap.parse_args(argv)
    if a.login_status:
        from .sites import bilibili
        return bilibili.login_status()
    if a.login_refresh:
        from .sites import bilibili
        return bilibili.refresh()
    if a.login:
        from .sites import bilibili
        return bilibili.login()          # 扫码登录，无需房间地址
    if a.url is None and a.mode != "serve-only":
        ap.error("需要房间地址(仅 --mode serve-only 可省)")
    if a.url:
        a.url = sites.canonical(a.url)   # 剥离分享链接的 tracking query(如抖音)
    return play_room(a.url, a)


def _serve_only(a):
    """只起常驻代理,不解析房间、不打开播放器。

    纯中转:不绑任何房间(即便给了地址也忽略),别处用 --mode serve 各带 ?room= 复用它播不同
    房间(裸连 /live.flv 会报错)。所有断流/转流日志都集中打印在本进程,方便同时从多个命令行
    启动多个播放。"""
    port, reuse = _choose_port(a.port)
    if reuse:
        print(f"已有代理在端口 {port} 运行,无需重复起(用 --port 指定别的端口可再起一个)。")
        return 0
    srv = subprocess.Popen([sys.executable, "-m", "live.server",
                            "",              # 纯中转:不绑房间(裸连报错);复用方都带 ?room=
                            str(port), a.quality or "",
                            "0"])            # 纯代理强制常驻:没有播放器生命周期可挂靠
    if not _wait_ready(port):
        print("警告:本地代理未在预期时间内就绪。")
    hint = "" if port == 8787 else f" --port {port}"   # 非默认端口才需在播放命令里带上
    print(f"本地代理已启动(端口 {port}),常驻。Ctrl+C 结束。")
    print("另开一个命令行,播放任意房间即会复用本代理(断流/转流日志都集中在这里):")
    print(f"    uv run cli <房间地址>{hint}")
    try:
        srv.wait()
    except KeyboardInterrupt:
        srv.terminate()
        srv.wait()   # 回收子进程(子进程已随进程组收到 SIGINT 自行优雅退出)
    return 0


def play_room(url, a):
    if a.mode == "serve-only":
        return _serve_only(a)   # 纯中转:忽略房间,不解析、不打开播放器

    info = sites.parse(url)
    headers = sites.play_headers(url)
    print(f"房间号 : {info['rid']}")
    print(f"主播   : {info['nick']}")
    print(f"标题   : {info['title']}")
    print(f"直播中 : {info['living']}")
    if not info["living"]:
        print("主播未开播。")
        return 1

    name, stream = common.pick(info, a.quality)
    if stream is None:
        print("该直播间未取到可播放的 flv 流(可能仅提供 HLS 或流结构异常)。")
        return 1
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
            _open_iina_m3u(info["rid"], title, flv, headers)  # 本地 m3u 让 IINA 显示标题
        print(f"已用直链打开 ({a.player})。注意:卡住无法自动恢复。")
        return 0

    if a.mode == "m3u":
        d = os.path.join(tempfile.gettempdir(), "MPV-LIVE")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{info['rid']}.m3u")
        with open(path, "w", encoding="utf-8") as f:
            f.write(common.m3u_content(title, stream))
        # m3u 里是 flv 直链，mpv 抓取时仍需 referer/UA。
        if a.player == "mpv":
            _open_mpv(path, title, headers)
        else:
            _open_iina(common.iina_url(title, path, headers))
        print(f"已用 m3u 播放列表打开:{path}\n卡住时在 IINA 播放列表切换「备用N」。")
        return 0

    # serve 模式:优先复用已有代理，否则在空闲端口新起。房间与清晰度都写进本地地址的
    # query（?room=&quality=），故复用别处已在跑的代理时也按本次请求解析、不受其启动参数限制。
    port, reuse = _choose_port(a.port)
    local = _serve_url(port, url, a.quality)

    srv = None
    if reuse:
        print(f"复用已有代理 (端口 {port})，无需新起。")
    else:
        srv = subprocess.Popen([sys.executable, "-m", "live.server",
                                url, str(port), a.quality or "", str(a.grace)])
        if not _wait_ready(port):
            print("警告:本地代理未在预期时间内就绪，仍尝试打开播放器。")
        print(f"本地代理已启动 (PID {srv.pid}，端口 {port})。")

    if a.player == "mpv":
        _open_mpv(local, title, {})   # 本地代理已带好平台头，mpv 直连 localhost 不需要
    else:
        # IINA 标题栏对网络直链只显示文件名，force-media-title 不生效；改用本地 m3u 靠 #EXTINF 名
        # 显示房间名。local 是 localhost 代理，referer/UA 由代理负责，不用带 headers。
        _open_iina_m3u(info["rid"], title, local)
    print(f"地址:{local}")

    if reuse:
        return 0   # 不占管现有代理，开完即返回
    print("直播断流由服务器自动重解析续播，播放器无感。Ctrl+C 结束。")
    try:
        srv.wait()
    except KeyboardInterrupt:
        srv.terminate()
        srv.wait()   # 回收子进程(子进程已随进程组收到 SIGINT 自行优雅退出)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
