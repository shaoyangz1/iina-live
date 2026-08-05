#!/usr/bin/env python3
"""平台派发层:按 URL 域名找到对应平台模块并解析。

新增平台 = 在本包内写一个模块(实现下面的接口)+ 在 SITES 里登记,server / cli 无需改动。

平台模块接口:
    DOMAINS       list[str]   匹配的域名关键字(如 ["huya.com"])
    PLAY_HEADERS  dict        拉流时用的 HTTP 头(Referer / User-Agent)
    parse(url)    -> dict     {rid, nick, title, living, streams{名:{quality,url,backups}}}
"""
import urllib.parse

from . import huya, douyin, douyu, bilibili, bangumi

# 已支持的平台模块(按需追加)。
# bangumi 的 DOMAINS 是宽泛的 "bilibili.com",必须排在 bilibili(直播,"live.bilibili.com")
# 之后,这样 live.bilibili.com 先命中直播,其余 www.bilibili.com 才落到番剧。
SITES = [huya, douyin, douyu, bilibili, bangumi]


def get_site(url: str):
    host = urllib.parse.urlparse(url).netloc.lower()
    for mod in SITES:
        if any(d in host for d in mod.DOMAINS):
            return mod
    raise RuntimeError(f"不支持的平台: {url}")


def parse(url: str) -> dict:
    return get_site(url).parse(url)


def play_headers(url: str) -> dict:
    return getattr(get_site(url), "PLAY_HEADERS", {})


def canonical(url: str) -> str:
    """规范化房间地址(如剥离分享链接的 tracking query)。平台可选实现 canonical(),默认原样返回。"""
    fn = getattr(get_site(url), "canonical", None)
    return fn(url) if fn else url


def is_vod(url: str) -> bool:
    """该地址是否点播(VOD,如 B 站番剧)。点播用 direct 打开,不启 serve 转流代理。"""
    return bool(getattr(get_site(url), "VOD", False))


def supported() -> list:
    """所有已支持的域名,用于提示。"""
    return [d for mod in SITES for d in mod.DOMAINS]
