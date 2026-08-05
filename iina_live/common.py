#!/usr/bin/env python3
"""平台无关的公共工具:HTTP、清晰度选择、iina/m3u 生成、reconnect 参数。

各平台解析模块(如 huya.py)与派发层(sites.py)共用这里的东西。
"""
import gzip
import hashlib
import urllib.parse
import urllib.request

# 完整 reconnect 开关(含 reconnect_at_eof)。很多平台的 flv 会周期性正常关闭连接(EOF),
# 只设 reconnect_streamed 不够,mpv 会当播放结束退出。
RECONNECT = ("reconnect=1,reconnect_streamed=1,reconnect_at_eof=1,"
             "reconnect_on_network_error=1,reconnect_delay_max=5")

# 通用桌面 UA,平台没特别要求时用它
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.3.1 Safari/605.1.15")


def _gunzip(raw: bytes) -> bytes:
    """部分接口即使未在头里声明也返回 gzip(magic 1f 8b),透明解压。"""
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def http_get(url, headers=None, timeout=15, data=None):
    """data 为 None 时 GET,否则 POST(bytes body);返回已透明解压的原始字节。"""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    return _gunzip(urllib.request.urlopen(req, timeout=timeout).read())


def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def pick(info: dict, quality: str = None):
    """选清晰度:quality 为 None 取最高(原画优先,quality==0 视为原画);
    否则按显示名或码率匹配。返回 (名称, stream)。"""
    streams = info["streams"]
    if not streams:
        return None, None
    if quality:
        for name, s in streams.items():
            if quality == name or quality == str(s["quality"]):
                return name, s
    name = max(streams, key=lambda k: (streams[k]["quality"] == 0, streams[k]["quality"]))
    return name, streams[name]


def _scheme(url: str, title: str, mpv_opts: dict) -> str:
    opts = {"force-media-title": title, "ytdl": "no", "stream-lavf-o": RECONNECT}
    opts.update(mpv_opts)
    q = ["url=" + urllib.parse.quote(url, safe="")]
    for k, v in opts.items():
        q.append(f"mpv_{k}=" + urllib.parse.quote(v, safe=""))
    return "iina://open?" + "&".join(q)


def _header_opts(headers: dict) -> dict:
    """把拉流 HTTP 头映射成对应的 mpv 选项(播放器直接拉流时需要)。"""
    opts = {}
    if headers:
        if headers.get("Referer"):
            opts["referrer"] = headers["Referer"]
        if headers.get("User-Agent"):
            opts["user-agent"] = headers["User-Agent"]
    return opts


def iina_url(title: str, flv: str, headers: dict = None, audio: str = None) -> str:
    """直链/含直链的本地文件(m3u):mpv 直接拉流,需带平台的 referer/UA。
    audio 非空时(DASH 点播,音视频分轨)作为独立音轨(mpv audio-file)一并交给播放器。"""
    opts = _header_opts(headers)
    if audio:
        opts["audio-file"] = audio
    return _scheme(flv, title, opts)


def iina_local_url(title: str, local_url: str, headers: dict = None, audio: str = None) -> str:
    """打开本地 m3u(靠 #EXTINF 名显示标题):
    - serve 模式:local_url 是 localhost 代理,referer/UA 由代理负责,headers 留空;
    - direct 模式:m3u 里是平台 CDN 直链,需带 referer/UA;DASH 点播再带 audio 音轨。"""
    opts = _header_opts(headers)
    if audio:
        opts["audio-file"] = audio
    return _scheme(local_url, title, opts)


def m3u_content(title: str, stream: dict) -> str:
    """多线路 m3u 播放列表(卡住可切备用线路);#EXTINF 名即 IINA 显示的标题。"""
    out = ["#EXTM3U"]
    urls = [stream["url"]] + stream["backups"]
    for i, u in enumerate(urls):
        out.append(f"#EXTINF:-1 ,{title}" + ("" if i == 0 else f" - 备用{i}"))
        out.append(u)
    return "\n".join(out)


def single_m3u(title: str, url: str) -> str:
    """单条 m3u:靠 #EXTINF 名让 IINA 显示标题(网络直链下 force-media-title 在 IINA 标题栏不生效)。"""
    return f"#EXTM3U\n#EXTINF:-1 ,{title}\n{url}\n"
