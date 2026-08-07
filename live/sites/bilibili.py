#!/usr/bin/env python3
"""哔哩哔哩直播(live.bilibili.com)平台解析模块。

平台模块统一接口见 sites/__init__.py。取流走 web getRoomPlayInfo 明文链路:
room_init 短号转真房号 → getRoomPlayInfo 拿多档多线路 flv。

直播取流无需 wbi 签名(与点播 x/player/wbi/playurl 不同,streamlink/yt-dlp/ihmily
现行做法均明文 query),纯 urllib+json 即可。原画/4K 需登录后取流:
- 扫码登录: `uv run cli --login bilibili`,cookie 存到本地(见 _cookie_path);或
没有 cookie 时请重新运行 `uv run cli --login bilibili`。
"""
import os
import json
import time
import datetime
import hashlib
import html
import pathlib
import re
import secrets
import urllib.parse
import urllib.request
import base64

from ..common import http_get

DOMAINS = ["live.bilibili.com"]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) "
      "Gecko/20100101 Firefox/127.0")
REFERER = "https://live.bilibili.com/"
PLAY_HEADERS = {"User-Agent": UA, "Referer": REFERER}
# 调 API 额外带 Origin,CDN 拉流只认 Referer(PLAY_HEADERS 已含)。
_API_HEADERS = {"User-Agent": UA, "Origin": "https://live.bilibili.com", "Referer": REFERER}

# qn 档位 → 显示名。quality 存 qn 数值:pick 默认取最大档(原画 10000),
# --quality 可按显示名或数值匹配。
_QN_NAME = {30000: "杜比", 20000: "4K", 10000: "原画", 400: "蓝光", 250: "超清", 150: "高清", 80: "流畅"}


def _get_json(url, cookie=None):
    """GET 返回 JSON;cookie 非空时带上(解锁原画/4K)。"""
    h = dict(_API_HEADERS)
    if cookie:
        h["Cookie"] = cookie
    return json.loads(http_get(url, headers=h))


def resolve_room(short, fetch=_get_json):
    """短号→(真房号 int, 开播?)。room_init 对短号/真号通用,顺带拿 live_status。

    fetch 可注入,测试用假响应驱动、不触网。"""
    d = fetch(f"https://api.live.bilibili.com/room/v1/Room/room_init?id={short}")["data"]
    return d["room_id"], d["live_status"] == 1


def _room_meta(rid, fetch=_get_json):
    """取标题/主播名;title 缺失回退主播名。

    getInfoByRoom 已被风控(-352,需 wbi/登录),改用免签名的 get_info 拿
    标题+uid,再 Master/info 按 uid 拿主播名。"""
    gi = fetch(f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={rid}")["data"]
    m = fetch(f"https://api.live.bilibili.com/live_user/v1/Master/info?uid={gi['uid']}")["data"]
    nick = (m.get("info") or {}).get("uname")
    title = gi.get("title") or nick
    return nick, title


def _streams_from_playinfo(data: dict) -> dict:
    """从 getRoomPlayInfo 的 data 提取各档流(纯函数,不触网)。

    完整地址 = host + base_url + extra;同一 codec 的 url_info 多条 = 多线路
    (首条主线,其余 backups)。stream 排序让 flv(http_stream)在前,同档
    已存则保留先到的 → 优先给 flv,hls 仅作补充。"""
    streams = {}
    playurl = (data.get("playurl_info") or {}).get("playurl") or {}
    stream_list = sorted(
        playurl.get("stream", []), key=lambda s: s.get("protocol_name") != "http_stream"
    )
    for stream in stream_list:
        for fmt in stream.get("format", []):
            for codec in fmt.get("codec", []):
                base = codec["base_url"]
                urls = [ui["host"] + base + ui.get("extra", "") for ui in codec["url_info"]]
                if not urls:
                    continue
                name = _QN_NAME.get(codec["current_qn"], str(codec["current_qn"]))
                if name not in streams:
                    streams[name] = {"quality": codec["current_qn"], "url": urls[0], "backups": urls[1:]}
    return streams


def parse(url: str) -> dict:
    """解析 B 站直播间,返回房间信息与各清晰度(多线路 backups)。"""
    short = urllib.parse.urlparse(url).path.strip("/").split("/")[0]
    rid, living = resolve_room(short)
    info = {"rid": str(rid), "nick": None, "title": None, "living": living, "streams": {}}
    if not living:
        return info

    info["nick"], info["title"] = _room_meta(rid)
    # 请求 qn=10000 原画;权限不足接口静默降档。codec=0 取 H.264,IINA/mpv 最稳。
    q = urllib.parse.urlencode({
        "room_id": rid, "protocol": "0,1", "format": "0,1,2", "codec": "0",
        "qn": 10000, "platform": "web", "ptype": 8,
    })
    data = _get_json(
        f"https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?{q}",
        _load_cookie(),
    )["data"]
    if data.get("live_status") == 0:  # room_init 到出流间隙下播
        info["living"] = False
        return info
    info["streams"] = _streams_from_playinfo(data)
    return info


# ---------------- 登录态 cookie ----------------
# 取流用到的关键 cookie(SESSDATA 解锁清晰度,其余登录态一并带上)
_WANT = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")
_COOKIE_INFO = "https://passport.bilibili.com/x/passport-login/web/cookie/info"
_COOKIE_REFRESH = "https://passport.bilibili.com/x/passport-login/web/cookie/refresh"
_CONFIRM_REFRESH = "https://passport.bilibili.com/x/passport-login/web/confirm/refresh"
_CORRESPOND = "https://www.bilibili.com/correspond/1/"
_RSA_N = "y4HdjgJHBlbaBN04VERG4qNBIFHP6a3GozCl75AihQloSWCXC5HDNgyinEnhaQ_4-gaMud_GF50elYXLlCToR9se9Z8z433U3KjM-3Yx7ptKkmQNAMggQwAVKgq3zYAoidNEWuxpkY_mAitTSRLnsJW-NCTa0bqBFF6Wm1MxgfE"
_RSA_E = 65537


def _cookie_path() -> pathlib.Path:
    """项目根目录的统一登录态文件路径。"""
    return pathlib.Path(__file__).resolve().parents[2] / ".cookie" / "bilibili"


def _refresh_token_path() -> pathlib.Path:
    return _cookie_path().with_name("bilibili.refresh_token")


def _load_cookie():
    """只读取项目根目录 `.cookie/bilibili`，没有文件则返回 None。"""
    p = _cookie_path()
    if p.exists():
        cookie = p.read_text(encoding="utf-8").strip()
        if cookie:
            return cookie
    return None


def _cookie_expiry(cookie):
    """从 cookie 串里的 SESSDATA 解析过期 Unix 时间戳(纯函数,不联网)。

    SESSDATA 是 URL 编码的「创建戳,过期戳,签名」三段,第二段即过期时间;解析不出返回 None。"""
    if not cookie:
        return None
    for kv in cookie.split(";"):
        kv = kv.strip()
        if kv.startswith("SESSDATA="):
            parts = urllib.parse.unquote(kv[len("SESSDATA="):]).split(",")
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1])
    return None


def _save_cookie(cookie: str) -> pathlib.Path:
    p = _cookie_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(cookie, encoding="utf-8")
    try:
        os.chmod(p, 0o600)   # cookie 含登录态,仅本人可读
    except OSError:
        pass
    return p


def _load_refresh_token() -> str | None:
    p = _refresh_token_path()
    if not p.exists():
        return None
    token = p.read_text(encoding="utf-8").strip()
    return token or None


def _save_refresh_token(token: str) -> pathlib.Path:
    p = _refresh_token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(token, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return p


def _cookies_from_setcookie(set_cookies) -> str:
    """从响应的 Set-Cookie 头列表拼出需要的 cookie 串(纯函数,便于测试)。"""
    got = {}
    for c in set_cookies:
        kv = c.split(";", 1)[0].strip()
        if "=" in kv:
            k, v = kv.split("=", 1)
            if k in _WANT:
                got[k] = v
    return "; ".join(f"{k}={got[k]}" for k in _WANT if k in got)


def _cookies_from_url(u: str) -> str:
    """从 poll 成功返回的跨域 url(query 里带 SESSDATA 等)拼出 cookie 串(纯函数)。"""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
    return "; ".join(f"{k}={q[k][0]}" for k in _WANT if k in q)


_QR_GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
_QR_POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?qrcode_key="


def _poll(qrcode_key: str):
    """轮询一次扫码状态,返回 (code, cookie串, refresh_token)。"""
    req = urllib.request.Request(_QR_POLL + qrcode_key, headers=_API_HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        set_cookies = r.headers.get_all("Set-Cookie") or []
        body = json.loads(r.read())
    d = body.get("data") or {}
    cookie = _cookies_from_url(d.get("url") or "") or _cookies_from_setcookie(set_cookies)
    return d.get("code"), cookie, d.get("refresh_token")


def _mgf1(seed: bytes, length: int) -> bytes:
    out = bytearray()
    for counter in range((length + 31) // 32):
        out.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
    return bytes(out[:length])


def _correspond_path(timestamp: int) -> str:
    """用标准库实现 B 站要求的 RSA-OAEP-SHA256 CorrespondPath。"""
    message = f"refresh_{timestamp}".encode()
    modulus = int.from_bytes(base64.urlsafe_b64decode(_RSA_N + "=" * (-len(_RSA_N) % 4)), "big")
    key_size = (modulus.bit_length() + 7) // 8
    digest = hashlib.sha256(b"").digest()
    seed = secrets.token_bytes(32)
    db = digest + b"\x00" * (key_size - len(message) - 2 * len(digest) - 2) + b"\x01" + message
    masked_db = bytes(a ^ b for a, b in zip(db, _mgf1(seed, key_size - len(digest) - 1)))
    masked_seed = bytes(a ^ b for a, b in zip(seed, _mgf1(masked_db, len(seed))))
    encoded = b"\x00" + masked_seed + masked_db
    encrypted = pow(int.from_bytes(encoded, "big"), _RSA_E, modulus).to_bytes(key_size, "big")
    return encrypted.hex()


def _cookie_value(cookie: str, name: str) -> str | None:
    for part in cookie.split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value
    return None


def _post_json(url: str, form: dict[str, str], cookie: str) -> tuple[dict, list[str]]:
    headers = dict(_API_HEADERS)
    headers.update({"Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded"})
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read()), r.headers.get_all("Set-Cookie") or []


def _refresh_csrf(cookie: str, timestamp: int) -> str | None:
    path = _correspond_path(timestamp)
    req = urllib.request.Request(_CORRESPOND + path, headers={**_API_HEADERS, "Cookie": cookie})
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read().decode("utf-8", "replace")
    match = re.search(r'<div\s+id=["\']1-name["\']>([^<]+)</div>', body)
    return html.unescape(match.group(1).strip()) if match else None


def refresh() -> int:
    """按 B 站 Web 流程刷新 cookie；没有 refresh_token 时要求重新扫码。"""
    cookie = _load_cookie()
    token = _load_refresh_token()
    if not cookie or not token:
        print("无法刷新：缺少登录凭据。请重新运行 `uv run cli --login bilibili`。")
        return 1
    try:
        info = _get_json(_COOKIE_INFO, cookie)
        data = info.get("data") or {}
        if not data.get("refresh"):
            print("当前无需刷新")
            return 0 if _print_login_info(cookie) else 1
        timestamp = int(data.get("timestamp") or time.time() * 1000)
        refresh_csrf = _refresh_csrf(cookie, timestamp)
        csrf = _cookie_value(cookie, "bili_jct")
        if not refresh_csrf or not csrf:
            print("刷新失败：未取得刷新令牌。请重新运行 `uv run cli --login bilibili`。")
            return 1
        result, set_cookies = _post_json(
            _COOKIE_REFRESH,
            {"csrf": csrf, "refresh_csrf": refresh_csrf, "source": "main_web", "refresh_token": token},
            cookie,
        )
        if result.get("code") != 0:
            print(f"刷新失败：{result.get('message') or result.get('code')}")
            return 1
        new_cookie = _cookies_from_setcookie(set_cookies)
        new_token = (result.get("data") or {}).get("refresh_token")
        if not new_cookie or not new_token:
            print("刷新失败：接口未返回完整登录凭据，请重新扫码。")
            return 1
        new_csrf = _cookie_value(new_cookie, "bili_jct")
        confirm, _ = _post_json(_CONFIRM_REFRESH, {"csrf": new_csrf or "", "refresh_token": token}, new_cookie)
        if confirm.get("code") != 0:
            print(f"刷新确认失败：{confirm.get('message') or confirm.get('code')}")
            return 1
        _save_cookie(new_cookie)
        _save_refresh_token(new_token)
        print("刷新成功")
        return 0 if _print_login_info(new_cookie) else 1
    except Exception as e:
        print(f"刷新失败：{e!r}")
        return 1


def login() -> int:
    """B 站扫码登录:拉二维码 → 终端打印 → 轮询确认 → cookie 落盘。返回退出码。"""
    from .. import qr

    gen = _get_json(_QR_GENERATE)
    if gen.get("code") != 0:
        print(f"获取登录二维码失败:{gen.get('message') or gen}")
        return 1
    key, url = gen["data"]["qrcode_key"], gen["data"]["url"]
    print(qr.terminal_qr(url))
    print("请用「哔哩哔哩」手机 App 扫描上面的二维码并确认登录。(Ctrl+C 取消)\n")

    deadline = time.time() + 180
    last = None
    while time.time() < deadline:
        try:
            code, cookie, refresh_token = _poll(key)
        except Exception as e:
            print(f"轮询出错:{e!r}")
            return 1
        if code == 0:
            if not cookie:
                print("登录成功但未取到 cookie(接口返回结构可能有变)。")
                return 1
            _save_cookie(cookie)
            if refresh_token:
                _save_refresh_token(refresh_token)
            print("登录成功")
            _print_login_info(cookie)
            return 0
        if code == 86038:
            print("二维码已失效,请重新运行登录。")
            return 1
        msg = {86101: "等待扫码…", 86090: "已扫码,请在手机上确认…"}.get(code, f"状态 {code}")
        if msg != last:
            print(msg, flush=True)
            last = msg
        time.sleep(2)
    print("登录超时(180s),请重试。")
    return 1


def _print_login_info(cookie: str) -> bool:
    """联网确认并打印用户信息;返回是否确认登录有效。"""
    try:
        nav = _get_json("https://api.bilibili.com/x/web-interface/nav", cookie).get("data", {})
    except Exception as e:
        print(f"cookie 已存,但查询登录态失败:{e!r}")
        return False
    if not nav.get("isLogin"):
        print("cookie 已失效(接口返回未登录)。请重新 `--login bilibili`。")
        return False
    vip = {0: "非大会员", 1: "大会员", 2: "年度大会员"}.get(nav.get("vipType"), "未知")
    print(f"已登录 : {nav.get('uname')}")
    print(f"会员   : {vip}")
    exp = _cookie_expiry(cookie)
    if exp:
        left = (exp - int(time.time())) / 86400
        when = datetime.datetime.fromtimestamp(exp).strftime("%Y-%m-%d")
        print(f"cookie : {when} 过期(剩余 {left:.0f} 天)")
    return True


def login_status() -> int:
    """查看 B 站登录状态:本地算 cookie 剩余有效期,联网确认登录态与会员类型。"""
    cookie = _load_cookie()
    if not cookie:
        print("未登录(无 cookie)。用 `--login bilibili` 扫码登录。")
        return 1
    if not _print_login_info(cookie):
        return 1
    return 0
