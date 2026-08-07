#!/usr/bin/env python3
"""哔哩哔哩番剧/影视(www.bilibili.com/bangumi/play/...)点播解析。

取流走 pgc 明文链路(无需 wbi 签名):
    season 接口(ep_id/season_id → 分集 cid/bvid) → pgc/player/web/playurl(fnval=4048 DASH) → 直链

fnval=4048 取 DASH(VIP 高清 1080P+/4K/HDR,音视频分轨);mp4(fnval=1)最高约 720P,仅作回退。
复用 live 的 HTTP 工具与 B 站登录 cookie(直播原画与番剧共用同一账号登录)。
"""
import re
import json
import urllib.parse

from live.common import http_get
from live.sites import bilibili as _live   # 复用 B 站登录 cookie(_load_cookie)

DOMAINS = ["bilibili.com"]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.3.1 Safari/605.1.15")
REFERER = "https://www.bilibili.com/"
PLAY_HEADERS = {"User-Agent": UA, "Referer": REFERER}

# qn 档位 → 显示名
_QN_NAME = {127: "8K", 126: "杜比视界", 125: "HDR", 120: "4K", 116: "1080P60",
            112: "1080P+", 80: "1080P", 74: "720P60", 64: "720P", 32: "480P", 16: "360P", 6: "240P"}


def _get_json(url, cookie=None):
    h = {"User-Agent": UA, "Referer": REFERER}
    if cookie:
        h["Cookie"] = cookie
    return json.loads(http_get(url, headers=h))


def resolve_id(url: str):
    """从番剧地址取 (kind, num):('ep', 123456) 或 ('ss', 28229)。"""
    slug = urllib.parse.urlparse(url).path.strip("/").split("/")[-1]
    m = re.match(r"(ep|ss)(\d+)", slug)
    if not m:
        raise RuntimeError("不是番剧地址(应形如 .../bangumi/play/ep123 或 ss123)")
    return m.group(1), int(m.group(2))


def _pick_episode(season: dict, kind: str, num: int, episode=None) -> dict:
    """从 season 的分集列表挑目标集(纯函数):
    - episode=='latest' → 取最后一个标题为数字的正片(避开列表尾的 PV);
    - episode 为正整数 → **按正片集号匹配**(ep.title == 该数字),同集号多条取时长最长=正片;
      匹配不到回退列表第 episode 项(超范围 {});
    - 否则 ep 地址精确匹配 ep_id,ss 地址取第一集(正片首集)。"""
    eps = season.get("episodes") or []
    if not eps:
        return {}
    if episode == "latest":
        last = max(
            (e for e in eps if str(e.get("title", "")).strip().isdigit()),
            key=lambda e: int(e["title"]), default=eps[-1])
        return last
    if episode is not None:
        hits = [e for e in eps if str(e.get("title", "")).strip() == str(episode)]
        if hits:
            return max(hits, key=lambda e: e.get("duration", 0) or 0)
        return eps[episode - 1] if 1 <= episode <= len(eps) else {}
    if kind == "ep":
        return next((e for e in eps if e.get("id") == num), eps[0])
    return eps[0]


def _streams_from_play(play: dict, qn_fallback: int = 0) -> dict:
    """mp4(fnval=1)回退:从 durl 取单段合并流(最高约 720P)。纯函数。"""
    durl = play.get("durl") or []
    if not durl:
        return {}
    qn = play.get("quality", qn_fallback)
    name = _QN_NAME.get(qn, str(qn))
    first = durl[0]
    return {name: {"quality": qn, "url": first["url"], "backups": list(first.get("backup_url") or [])}}


def _base(x: dict) -> str:
    return x.get("baseUrl") or x.get("base_url") or ""


def _streams_from_dash(dash: dict) -> dict:
    """从 DASH 提取各档流(纯函数,不触网):VIP 高清(1080P+/4K/HDR)都在这里。

    音视频分轨:每档=一条视频轨 + 一条(全局最高码率)音频轨,播放时音频作 audio-file 合并。
    同一清晰度(qn=id)常有多 codec,优先 H.264(avc1)以求最广兼容,没有再取第一条。"""
    videos = dash.get("video") or []
    audios = dash.get("audio") or []
    if not videos or not audios:
        return {}
    audio_url = _base(max(audios, key=lambda a: a.get("bandwidth", 0)))
    streams = {}
    for qn in sorted({v.get("id") for v in videos}, reverse=True):
        cands = [v for v in videos if v.get("id") == qn]
        v = next((x for x in cands if (x.get("codecs") or "").startswith("avc")), cands[0])
        name = _QN_NAME.get(qn, str(qn))
        if name not in streams:
            streams[name] = {
                "quality": qn,
                "url": _base(v),
                "backups": list(v.get("backupUrl") or v.get("backup_url") or []),
                "audio": audio_url,
            }
    return streams


def get_season_info(url: str) -> dict:
    """只取番剧基本信息和分集列表(不取播放流)。"""
    kind, num = resolve_id(url)
    key = "ep_id" if kind == "ep" else "season_id"
    season = _get_json(f"https://api.bilibili.com/pgc/view/web/season?{key}={num}")["result"]
    eps = season.get("episodes") or []
    out = []
    for e in eps:
        out.append({
            "id": e.get("id"), "cid": e.get("cid"), "bvid": e.get("bvid", ""),
            "title": e.get("title") or "", "long_title": e.get("long_title") or "",
            "duration": e.get("duration", 0) or 0,
        })
    return {
        "kind": kind, "num": num,
        "nick": season.get("season_title") or season.get("title"),
        "episodes": out,
        "season_id": season.get("season_id"),
    }


def parse(url: str, episode: int = None) -> dict:
    """解析番剧,返回信息 + 各档流(DASH:视频轨 + audio 音轨)。

    episode(1 起或 'latest')显式选集;不给则按 ep 地址精确/ss 首集。info 额外带
    episodes(总集数)、season_id(整季 ss 号,给 ep 地址时可反查)。"""
    kind, num = resolve_id(url)
    key = "ep_id" if kind == "ep" else "season_id"
    season = _get_json(f"https://api.bilibili.com/pgc/view/web/season?{key}={num}")["result"]
    eps = season.get("episodes") or []
    ep = _pick_episode(season, kind, num, episode)
    info = {
        "rid": f"{kind}{num}",
        "nick": season.get("season_title") or season.get("title"),
        "title": None,
        "living": bool(ep),
        "streams": {},
        "episodes": len(eps),
        "season_id": season.get("season_id"),
    }
    if not ep:
        return info
    parts = [season.get("season_title") or "", ep.get("title") or "", ep.get("long_title") or ""]
    info["title"] = " ".join(p for p in parts if p) or info["nick"]
    # fnval=4048 → DASH(VIP 高清,音视频分轨);fourk=1 放行 4K;大会员正片需登录 cookie
    q = urllib.parse.urlencode({
        "cid": ep["cid"], "bvid": ep.get("bvid", ""), "qn": 127,
        "fnver": 0, "fnval": 4048, "fourk": 1,
    })
    play = _get_json(
        f"https://api.bilibili.com/pgc/player/web/playurl?{q}", _live._load_cookie()
    )
    if play.get("code") == -10403:
        raise RuntimeError("该内容需要大会员/地区限制;请先用 `uv run cli --login bilibili` 登录会员账号")
    result = play.get("result") or {}
    info["streams"] = _streams_from_dash(result.get("dash") or {}) or _streams_from_play(result)
    return info
