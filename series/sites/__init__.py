#!/usr/bin/env python3
"""点播平台派发层:按 URL 域名找到对应平台模块并解析。

平台模块接口:
    DOMAINS       list[str]   匹配的域名关键字
    PLAY_HEADERS  dict        拉流 HTTP 头(Referer / User-Agent)
    parse(url, episode=None) -> dict  {rid, nick, title, living, streams{名:{quality,url,backups,audio?}},
                                       episodes?, season_id?}
"""
import urllib.parse

from . import bilibili

# 已支持的点播平台(按需追加)
SITES = [bilibili]


def get_site(url: str):
    host = urllib.parse.urlparse(url).netloc.lower()
    for mod in SITES:
        if any(d in host for d in mod.DOMAINS):
            return mod
    raise RuntimeError(f"不支持的点播平台: {url}")


def parse(url: str, episode=None) -> dict:
    return get_site(url).parse(url, episode=episode)


def get_season_info(url: str) -> dict:
    return get_site(url).get_season_info(url)


def play_headers(url: str) -> dict:
    return getattr(get_site(url), "PLAY_HEADERS", {})
