#!/usr/bin/env python3
"""哔哩哔哩番剧/影视(www.bilibili.com/bangumi/play/...)解析模块。

注意:番剧是**点播(VOD)**,不是直播。取流走 pgc 明文链路(与直播的 getRoomPlayInfo
类似,同样无需 wbi 签名):
    season 接口(ep_id/season_id → 分集 cid/bvid) → pgc/player/web/playurl(fnval=1 mp4) → durl 直链

用 fnval=1 取 **mp4 合并流**(单文件,音视频不分轨),直接套 direct 模式交给 IINA/mpv 播放,
不走 server 转流代理(点播是完整文件,没有断流续播的问题)。大会员/付费正片需登录:
复用 `--login bilibili` 存的 cookie(见 bilibili._load_cookie)。

VOD=True 告诉 cli 这是点播 → 默认用 direct 打开。
"""
import re
import json
import urllib.parse

from ..common import http_get
from . import bilibili

DOMAINS = ["bilibili.com"]     # 放在 live.bilibili.com(直播)之后登记,故只接到 www 番剧
VOD = True                     # 点播:cli 见此默认走 direct,不启 serve 代理
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.3.1 Safari/605.1.15")
REFERER = "https://www.bilibili.com/"
PLAY_HEADERS = {"User-Agent": UA, "Referer": REFERER}

# qn 档位 → 显示名(番剧/影视);mp4(fnval=1)通常最高到 1080P。
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
    - episode=='latest' → 最后一集(最新);
    - episode 为正整数(1 起)→ 第 episode 集(超范围返回 {});
    - 否则 ep 地址精确匹配 ep_id,ss 地址取第一集(正片首集)。"""
    eps = season.get("episodes") or []
    if not eps:
        return {}
    if episode == "latest":
        return eps[-1]
    if episode is not None:
        return eps[episode - 1] if 1 <= episode <= len(eps) else {}
    if kind == "ep":
        return next((e for e in eps if e.get("id") == num), eps[0])
    return eps[0]


def _streams_from_play(play: dict, qn_fallback: int = 0) -> dict:
    """从 playurl 的 result 提取单档流(纯函数,不触网)。

    fnval=1 的 mp4 通常单段:取 durl[0] 主 url + backup_url 作备用线路(多 CDN)。
    番剧偶有分段(durl 多个)时仅取首段,其余忽略(正片多为单段)。"""
    durl = play.get("durl") or []
    if not durl:
        return {}
    qn = play.get("quality", qn_fallback)
    name = _QN_NAME.get(qn, str(qn))
    first = durl[0]
    backups = list(first.get("backup_url") or [])
    return {name: {"quality": qn, "url": first["url"], "backups": backups}}


def parse(url: str, episode: int = None) -> dict:
    """解析番剧,返回房间式信息 + 单档 mp4 直链(套 direct 模式)。

    episode(1 起)显式选集;不给则按 ep 地址精确/ss 首集。info 额外带 episodes(总集数),
    供 cli 提示选集。"""
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
        "episodes": len(eps),                       # 总集数(番剧特有),cli 用来提示选集
        "season_id": season.get("season_id"),       # 整季 ss 号(给 ep 地址时可反查出 ss)
    }
    if not ep:
        return info
    # 标题:季名 + 分集短标题(如「第1话 XXX」)
    parts = [season.get("season_title") or "", ep.get("title") or "", ep.get("long_title") or ""]
    info["title"] = " ".join(p for p in parts if p) or info["nick"]
    # 取流:fnval=1 → mp4 合并流;qn=112 尽量高,无权限接口自动降档
    q = urllib.parse.urlencode({
        "cid": ep["cid"], "bvid": ep.get("bvid", ""), "qn": 112,
        "fnver": 0, "fnval": 1, "fourk": 1,
    })
    play = _get_json(
        f"https://api.bilibili.com/pgc/player/web/playurl?{q}", bilibili._load_cookie()
    )
    if play.get("code") == -10403:
        raise RuntimeError("该内容需要大会员;请先 `--login bilibili` 用会员账号登录")
    info["streams"] = _streams_from_play(play.get("result") or {})
    return info
