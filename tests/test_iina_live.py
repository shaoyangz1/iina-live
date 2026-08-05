#!/usr/bin/env python3
"""iina-live 纯函数单元测试:标准库 unittest,零依赖、不触网。

覆盖:清晰度选择、m3u 生成、虎牙签名(uid 移位 / wsSecret)、gzip 解压、
serve 代理按路径解析房间,以及抖音/斗鱼/B 站三平台的解析纯函数与派发路由。
触网部分(各平台 parse 的 HTTP 请求)不在测试范围,均通过注入 fetch /
直接喂假 payload 的方式绕开。

    python -m unittest tests.test_iina_live
"""
import os
import base64
import hashlib
import json as _json
import unittest
import urllib.parse

from iina_live import common, server, sites, cli, qr
from iina_live.sites import huya, douyin, douyu, bilibili


def _stream(quality, url="u0", backups=("u1", "u2")):
    return {"quality": quality, "url": url, "backups": list(backups)}


class TestPick(unittest.TestCase):
    def test_empty_streams_returns_none(self):
        self.assertEqual(common.pick({"streams": {}}), (None, None))

    def test_default_picks_highest_bitrate(self):
        info = {"streams": {"高清": _stream(500), "蓝光": _stream(2000)}}
        name, s = common.pick(info)
        self.assertEqual(name, "蓝光")
        self.assertEqual(s["quality"], 2000)

    def test_default_prefers_yuanhua_quality_zero(self):
        # quality==0 视为原画,应优先于任何正码率
        info = {"streams": {"原画": _stream(0), "蓝光": _stream(2000)}}
        name, _ = common.pick(info)
        self.assertEqual(name, "原画")

    def test_pick_by_display_name(self):
        info = {"streams": {"高清": _stream(500), "蓝光": _stream(2000)}}
        name, _ = common.pick(info, "高清")
        self.assertEqual(name, "高清")

    def test_pick_by_bitrate_string(self):
        info = {"streams": {"高清": _stream(500), "蓝光": _stream(2000)}}
        name, _ = common.pick(info, "2000")
        self.assertEqual(name, "蓝光")

    def test_unknown_quality_falls_back_to_highest(self):
        info = {"streams": {"高清": _stream(500), "蓝光": _stream(2000)}}
        name, _ = common.pick(info, "不存在")
        self.assertEqual(name, "蓝光")


class TestM3U(unittest.TestCase):
    def test_layout_with_backups(self):
        content = common.m3u_content("房间", _stream(0, "u0", ["u1", "u2"]))
        self.assertEqual(
            content.splitlines(),
            [
                "#EXTM3U",
                "#EXTINF:-1 ,房间",
                "u0",
                "#EXTINF:-1 ,房间 - 备用1",
                "u1",
                "#EXTINF:-1 ,房间 - 备用2",
                "u2",
            ],
        )

    def test_single_line_no_backups(self):
        content = common.m3u_content("A", _stream(0, "only", []))
        self.assertEqual(content.splitlines(), ["#EXTM3U", "#EXTINF:-1 ,A", "only"])

    def test_single_m3u_layout(self):
        self.assertEqual(
            common.single_m3u("标题", "http://127.0.0.1:8787/live.flv").splitlines(),
            ["#EXTM3U", "#EXTINF:-1 ,标题", "http://127.0.0.1:8787/live.flv"],
        )

    def test_iina_local_url_plain(self):
        u = common.iina_local_url("标题", "/tmp/x.m3u")
        self.assertTrue(u.startswith("iina://open?"))
        self.assertIn("mpv_force-media-title=", u)   # 靠 #EXTINF 显示标题,但仍带上
        self.assertNotIn("mpv_audio-file=", u)

    def test_iina_local_url_with_headers_and_audio(self):
        # direct 模式:本地 m3u + referer/UA + DASH 音轨
        u = common.iina_local_url("标题", "/tmp/x.m3u",
                                  {"Referer": "https://www.bilibili.com/", "User-Agent": "UA"},
                                  "https://cdn/audio.m4s")
        self.assertIn("mpv_referrer=", u)
        self.assertIn("mpv_user-agent=", u)
        self.assertIn("mpv_audio-file=", u)


class TestHttpHelpers(unittest.TestCase):
    def test_gunzip_passthrough_plain(self):
        self.assertEqual(common._gunzip(b'{"a":1}'), b'{"a":1}')

    def test_gunzip_decompresses_gzip(self):
        import gzip as _gz

        self.assertEqual(common._gunzip(_gz.compress(b"hello")), b"hello")


class TestRotUid(unittest.TestCase):
    @staticmethod
    def _reference(uid):
        # 独立参考实现:高 32 位不变,低 32 位循环左移 8 位
        hi = (uid >> 32) & 0xFFFFFFFF
        lo = uid & 0xFFFFFFFF
        rotl = ((lo << 8) | (lo >> 24)) & 0xFFFFFFFF
        return (hi << 32) | rotl

    def test_matches_reference(self):
        for uid in (0, 1, 0x12345678, 0xDEADBEEF, 0x00000001FF00AB00, 4294967294):
            self.assertEqual(huya._rot_uid(uid), self._reference(uid), f"uid={uid:#x}")

    def test_zero(self):
        self.assertEqual(huya._rot_uid(0), 0)


class TestWsSecret(unittest.TestCase):
    def test_matches_independent_recompute(self):
        fm_plain = "prefix_$0_$1_$2_$3"
        fm_enc = urllib.parse.quote(base64.b64encode(fm_plain.encode()).decode())
        anti = {"fm": fm_enc, "wsTime": "5f000000", "ctype": "huya_live", "t": "100"}
        convert_uid, seqid, stream_name = 123456, 987654321, "someStream-1"

        got = huya._ws_secret(anti, convert_uid, seqid, stream_name)

        # 独立复算(照 wsSecret 公开算法),锁定当前实现
        s = hashlib.md5(f"{seqid}|huya_live|100".encode()).hexdigest()
        u = f"prefix_{convert_uid}_{stream_name}_{s}_5f000000"
        expected = hashlib.md5(u.encode()).hexdigest()
        self.assertEqual(got, expected)

    def test_default_t_when_missing(self):
        # anti 无 t 时默认 "100"
        fm_plain = "p_$0_$1_$2_$3"
        anti = {
            "fm": urllib.parse.quote(base64.b64encode(fm_plain.encode()).decode()),
            "wsTime": "abc",
            "ctype": "c",
        }
        got = huya._ws_secret(anti, 1, 2, "n")
        s = hashlib.md5("2|c|100".encode()).hexdigest()
        expected = hashlib.md5(f"p_1_n_{s}_abc".encode()).hexdigest()
        self.assertEqual(got, expected)


class TestParseRequest(unittest.TestCase):
    """serve 代理按请求解析 (room, quality):?room=/?quality= 优先,回退路径 slug 与全局默认。"""

    def setUp(self):
        server.ROOM = "https://www.huya.com/lpl"
        server._ORIGIN = "https://www.huya.com/"
        server.QUALITY = None

    def test_slug_path_uses_default_platform(self):
        self.assertEqual(server.parse_request("/lpl.flv"), ("https://www.huya.com/lpl", None))

    def test_numeric_slug(self):
        self.assertEqual(server.parse_request("/660000.flv"), ("https://www.huya.com/660000", None))

    def test_live_or_root_uses_default_room(self):
        self.assertEqual(server.parse_request("/live.flv")[0], "https://www.huya.com/lpl")
        self.assertEqual(server.parse_request("/")[0], "https://www.huya.com/lpl")

    def test_full_http_path_used_directly(self):
        self.assertEqual(
            server.parse_request("/https://live.bilibili.com/123")[0],
            "https://live.bilibili.com/123",
        )

    def test_room_query_overrides_path_cross_platform(self):
        room = "https://live.bilibili.com/123"
        path = "/live.flv?room=" + urllib.parse.quote(room, safe="")
        self.assertEqual(server.parse_request(path)[0], room)

    def test_quality_query_parsed(self):
        room, quality = server.parse_request("/lpl.flv?quality=" + urllib.parse.quote("原画"))
        self.assertEqual((room, quality), ("https://www.huya.com/lpl", "原画"))

    def test_quality_query_overrides_global_default(self):
        server.QUALITY = "蓝光"
        self.assertEqual(server.parse_request("/lpl.flv?quality=原画")[1], "原画")

    def test_quality_falls_back_to_global_default(self):
        server.QUALITY = "蓝光"
        self.assertEqual(server.parse_request("/lpl.flv")[1], "蓝光")

    def test_no_default_room_bare_connect_empty(self):
        # serve-only 裸代理:无默认房间时裸连 /live.flv 与 / 解析为空(do_GET 据此报 400),
        # 但带房间号/别名(/lpl.flv)或 ?room= 仍正常解析。
        server.ROOM = None
        self.assertFalse(server.parse_request("/live.flv")[0])
        self.assertFalse(server.parse_request("/")[0])
        self.assertTrue(server.parse_request("/lpl.flv")[0])


class TestServeUrl(unittest.TestCase):
    """cli._serve_url 生成的地址把 room/quality 写进 query,并与 server.parse_request 闭环一致。"""

    def setUp(self):
        server.ROOM = "https://www.huya.com/lpl"
        server._ORIGIN = "https://www.huya.com/"
        server.QUALITY = None

    def test_room_only_when_no_quality(self):
        url = cli._serve_url(8787, "https://www.huya.com/lpl", None)
        pr = urllib.parse.urlparse(url)
        self.assertEqual((pr.netloc, pr.path), ("127.0.0.1:8787", "/live.flv"))
        qs = urllib.parse.parse_qs(pr.query)
        self.assertEqual(qs.get("room"), ["https://www.huya.com/lpl"])
        self.assertNotIn("quality", qs)

    def test_roundtrips_through_parse_request(self):
        room = "https://live.bilibili.com/123"
        pr = urllib.parse.urlparse(cli._serve_url(9000, room, "原画"))
        self.assertEqual(server.parse_request(pr.path + "?" + pr.query), (room, "原画"))

    def test_room_url_stays_readable(self):
        url = cli._serve_url(8787, "https://www.huya.com/lpl", None)
        self.assertIn("room=https://www.huya.com/lpl", url)


# ---------- 抖音 ----------
def _enter(status=2, with_sdk=True):
    room = {"status": status, "title": "早安", "owner": {"nickname": "主播A"}}
    stream_url = {"flv_pull_url": {"FULL_HD1": "http://x/full.flv", "HD1": "http://x/hd.flv"}}
    if with_sdk:
        stream_url["live_core_sdk_data"] = {
            "pull_data": {
                "options": {
                    "qualities": [
                        {"name": "原画", "sdk_key": "origin", "v_bit_rate": 0},
                        {"name": "高清", "sdk_key": "sd", "v_bit_rate": 1000},
                    ]
                },
                "stream_data": _json.dumps(
                    {
                        "data": {
                            "origin": {"main": {"flv": "http://x/origin.flv"}},
                            "sd": {"main": {"flv": "http://x/sd.flv"}},
                        }
                    }
                ),
            }
        }
    room["stream_url"] = stream_url
    return {"data": {"data": [room], "user": {"nickname": "主播A"}}}


class TestDouyinResolveWebRid(unittest.TestCase):
    def test_last_path_segment(self):
        self.assertEqual(douyin.resolve_web_rid("https://live.douyin.com/123456"), "123456")


class TestDouyinParseEnter(unittest.TestCase):
    def test_living_with_sdk_qualities(self):
        info = douyin._parse_enter(_enter(), "123456")
        self.assertTrue(info["living"])
        self.assertEqual(info["rid"], "123456")
        self.assertEqual(info["nick"], "主播A")
        self.assertEqual(info["title"], "早安")
        self.assertEqual(
            info["streams"]["原画"], {"quality": 0, "url": "http://x/origin.flv", "backups": []}
        )
        self.assertEqual(info["streams"]["高清"]["quality"], 1000)
        self.assertEqual(info["streams"]["高清"]["url"], "http://x/sd.flv")

    def test_not_living_empty_streams(self):
        info = douyin._parse_enter(_enter(status=0), "1")
        self.assertFalse(info["living"])
        self.assertEqual(info["streams"], {})

    def test_fallback_to_flv_pull_url(self):
        info = douyin._parse_enter(_enter(with_sdk=False), "1")
        self.assertTrue(info["living"])
        self.assertEqual(info["streams"]["原画"]["url"], "http://x/full.flv")
        self.assertEqual(info["streams"]["高清"]["url"], "http://x/hd.flv")
        self.assertEqual(info["streams"]["原画"]["backups"], [])

    def test_invalid_stream_data_falls_back_to_flv_pull_url(self):
        p = _enter(with_sdk=True)
        p["data"]["data"][0]["stream_url"]["live_core_sdk_data"]["pull_data"]["stream_data"] = "NOT_JSON"
        info = douyin._parse_enter(p, "1")
        self.assertTrue(info["living"])
        self.assertEqual(info["streams"]["原画"]["url"], "http://x/full.flv")

    def test_pull_data_null_falls_back_to_flv_pull_url(self):
        p = _enter(with_sdk=False)
        p["data"]["data"][0]["stream_url"]["live_core_sdk_data"] = {"pull_data": None}
        info = douyin._parse_enter(p, "1")
        self.assertTrue(info["living"])
        self.assertEqual(info["streams"]["原画"]["url"], "http://x/full.flv")

    def test_options_null_falls_back_to_flv_pull_url(self):
        p = _enter(with_sdk=False)
        p["data"]["data"][0]["stream_url"]["live_core_sdk_data"] = {"pull_data": {"options": None}}
        info = douyin._parse_enter(p, "1")
        self.assertTrue(info["living"])
        self.assertEqual(info["streams"]["原画"]["url"], "http://x/full.flv")

    def test_living_true_but_no_flv_streams_empty(self):
        payload = {"data": {"data": [{"status": 2, "title": "t", "stream_url": {}}],
                            "user": {"nickname": "n"}}}
        info = douyin._parse_enter(payload, "1")
        self.assertTrue(info["living"])
        self.assertEqual(info["streams"], {})


def _ssr_block(obj):
    """把 dict 包成一个抖音 SSR flight 块:JSON 双重转义后塞进 pace_f script。"""
    inner = _json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    literal = _json.dumps(inner, ensure_ascii=False)  # 再转义为 JS 字符串字面量(带 \")
    return f"<script>self.__pace_f.push([1,{literal}])</script>"


class TestDouyinRoomFromHtml(unittest.TestCase):
    def _html(self, rid="999", status=2):
        empty = _ssr_block({"roomStore": {"roomInfo": {}}})
        real = _ssr_block(
            {"roomStore": {"roomInfo": {"room": {
                "status": status, "title": "标题",
                "owner": {"nickname": "主播", "web_rid": rid},
                "stream_url": {"flv_pull_url": {"FULL_HD1": "http://x/f.flv"}},
            }}}}
        )
        return f"<html>{empty}{real}</html>"

    def test_extract_skips_empty_shell(self):
        room = douyin._room_from_html(self._html(rid="999"), "999")
        self.assertEqual(room["status"], 2)
        self.assertEqual(room["title"], "标题")

    def test_missing_web_rid_returns_empty(self):
        self.assertEqual(douyin._room_from_html(self._html(rid="888"), "999"), {})

    def test_extracted_room_feeds_parse_enter(self):
        room = douyin._room_from_html(self._html(rid="999"), "999")
        info = douyin._parse_enter({"data": {"data": [room], "user": {}}}, "999")
        self.assertTrue(info["living"])
        self.assertEqual(info["nick"], "主播")
        self.assertEqual(info["streams"]["原画"]["url"], "http://x/f.flv")


class TestCanonical(unittest.TestCase):
    def test_douyin_strips_tracking_query(self):
        u = ("https://live.douyin.com/870887192950?enter_from_merge=link_share"
             "&enter_method=copy_link_share&action_type=click&from=web_code_link")
        self.assertEqual(sites.canonical(u), "https://live.douyin.com/870887192950")

    def test_douyin_plain_url_unchanged(self):
        u = "https://live.douyin.com/870887192950"
        self.assertEqual(sites.canonical(u), u)

    def test_default_platform_keeps_query(self):
        # 斗鱼未实现 canonical → 原样(需保留 ?rid=)
        u = "https://www.douyu.com/topic/x?rid=888"
        self.assertEqual(sites.canonical(u), u)

    def test_huya_default_unchanged(self):
        u = "https://www.huya.com/lpl"
        self.assertEqual(sites.canonical(u), u)


class TestDouyinDispatch(unittest.TestCase):
    def test_get_site_routes_to_douyin(self):
        self.assertIs(sites.get_site("https://live.douyin.com/123456"), douyin)

    def test_play_headers_has_referer(self):
        h = sites.play_headers("https://live.douyin.com/123456")
        self.assertEqual(h["Referer"], "https://live.douyin.com/")


# ---------- 斗鱼 ----------
class TestDouyuAuth(unittest.TestCase):
    @staticmethod
    def _ref(rand_str, key, enc_time, is_special, rid, ts):
        s = rand_str
        for _ in range(enc_time):
            s = hashlib.md5((s + key).encode()).hexdigest()
        salt = "" if is_special == 1 else f"{rid}{ts}"
        return hashlib.md5((s + key + salt).encode()).hexdigest()

    def test_special_salt_empty(self):
        enc = {"rand_str": "abc", "key": "k1", "enc_time": 3, "is_special": 1}
        self.assertEqual(
            douyu._auth(enc, "9527", 1700000000),
            self._ref("abc", "k1", 3, 1, "9527", 1700000000),
        )

    def test_nonspecial_salt_rid_ts(self):
        enc = {"rand_str": "xyz", "key": "k2", "enc_time": 5, "is_special": 0}
        self.assertEqual(
            douyu._auth(enc, "6666", 1700000001),
            self._ref("xyz", "k2", 5, 0, "6666", 1700000001),
        )

    def test_is_special_changes_result(self):
        base = {"rand_str": "abc", "key": "k1", "enc_time": 3}
        a = douyu._auth({**base, "is_special": 1}, "9527", 1700000000)
        b = douyu._auth({**base, "is_special": 0}, "9527", 1700000000)
        self.assertNotEqual(a, b)


class TestDouyuResolveRid(unittest.TestCase):
    @staticmethod
    def _boom(url):  # 数字/带 rid 时不应触发抓取
        raise AssertionError(f"不应抓取: {url}")

    def test_numeric_path(self):
        self.assertEqual(douyu.resolve_rid("https://www.douyu.com/123456", fetch=self._boom), "123456")

    def test_query_rid(self):
        self.assertEqual(
            douyu.resolve_rid("https://www.douyu.com/topic/x?rid=888", fetch=self._boom), "888"
        )

    def test_alias_pat_vipid(self):
        html = 'foo "rid":9527,"vipId":0 bar'
        self.assertEqual(douyu.resolve_rid("https://www.douyu.com/lpl", fetch=lambda u: html), "9527")

    def test_alias_pat_roominfo(self):
        html = 'x "roomInfo":{"rid":6666,"name":"y"} z'
        self.assertEqual(douyu.resolve_rid("https://www.douyu.com/king", fetch=lambda u: html), "6666")

    def test_not_found_raises(self):
        with self.assertRaises(RuntimeError):
            douyu.resolve_rid("https://www.douyu.com/nobody", fetch=lambda u: "no room here")


class TestDouyuPlayUrl(unittest.TestCase):
    def test_join_strips_trailing_slash(self):
        data = {"rtmp_url": "https://d.com/live/", "rtmp_live": "abc.flv?t=1"}
        self.assertEqual(douyu._play_url(data), "https://d.com/live/abc.flv?t=1")


class TestDouyuRoomFromBetard(unittest.TestCase):
    def test_living(self):
        bet = {"room": {"nickname": "主播", "room_name": "标题", "show_status": 1, "videoLoop": 0}}
        r = douyu._room_from_betard(bet)
        self.assertEqual((r["nick"], r["title"], r["living"]), ("主播", "标题", True))

    def test_off(self):
        bet = {"room": {"nickname": "主播", "show_status": 2, "videoLoop": 0}}
        self.assertFalse(douyu._room_from_betard(bet)["living"])

    def test_loop_not_living(self):
        bet = {"room": {"nickname": "主播", "show_status": 1, "videoLoop": 1}}
        self.assertFalse(douyu._room_from_betard(bet)["living"])

    def test_title_falls_back_to_nick(self):
        bet = {"room": {"nickname": "主播", "show_status": 1, "videoLoop": 0}}
        self.assertEqual(douyu._room_from_betard(bet)["title"], "主播")


class TestDouyuDispatch(unittest.TestCase):
    def test_get_site_routes_to_douyu(self):
        self.assertIs(sites.get_site("https://www.douyu.com/123456"), douyu)

    def test_play_headers_has_referer(self):
        h = sites.play_headers("https://www.douyu.com/123456")
        self.assertEqual(h["Referer"], "https://www.douyu.com")


# ---------- 哔哩哔哩 ----------
class TestBiliResolveRoom(unittest.TestCase):
    def test_short_to_real_and_living(self):
        fake = {"data": {"room_id": 654321, "uid": 1, "live_status": 1}}
        self.assertEqual(bilibili.resolve_room(123, fetch=lambda u: fake), (654321, True))

    def test_not_living(self):
        fake = {"data": {"room_id": 100, "uid": 1, "live_status": 0}}
        self.assertEqual(bilibili.resolve_room(100, fetch=lambda u: fake), (100, False))

    def test_loop_not_living(self):
        fake = {"data": {"room_id": 100, "uid": 1, "live_status": 2}}
        self.assertFalse(bilibili.resolve_room(100, fetch=lambda u: fake)[1])


class TestBiliRoomMeta(unittest.TestCase):
    def _fetch(self, title="标题", uname="主播"):
        def f(url):
            if "get_info" in url:
                return {"data": {"title": title, "uid": 999}}
            return {"data": {"info": {"uname": uname}}}
        return f

    def test_title_and_nick(self):
        self.assertEqual(bilibili._room_meta(1, fetch=self._fetch()), ("主播", "标题"))

    def test_title_falls_back_to_nick(self):
        self.assertEqual(bilibili._room_meta(1, fetch=self._fetch(title="")), ("主播", "主播"))


class TestBiliStreamsFromPlayinfo(unittest.TestCase):
    def _data(self):
        codec = {
            "codec_name": "avc", "current_qn": 10000, "accept_qn": [10000, 400],
            "base_url": "/live/123.flv?p=1",
            "url_info": [
                {"host": "https://c1.bili.com", "extra": "&k=a"},
                {"host": "https://c2.bili.com", "extra": "&k=b"},
            ],
        }
        return {"playurl_info": {"playurl": {"stream": [
            {"protocol_name": "http_stream", "format": [{"format_name": "flv", "codec": [codec]}]},
        ]}}}

    def test_join_and_backups(self):
        s = bilibili._streams_from_playinfo(self._data())["原画"]
        self.assertEqual(s["quality"], 10000)
        self.assertEqual(s["url"], "https://c1.bili.com/live/123.flv?p=1&k=a")
        self.assertEqual(s["backups"], ["https://c2.bili.com/live/123.flv?p=1&k=b"])

    def test_flv_preferred_over_hls_same_qn(self):
        d = self._data()
        hls_codec = {"current_qn": 10000, "base_url": "/live/123.m3u8",
                     "url_info": [{"host": "https://h.bili.com", "extra": ""}]}
        d["playurl_info"]["playurl"]["stream"].insert(
            0, {"protocol_name": "http_hls", "format": [{"format_name": "fmp4", "codec": [hls_codec]}]})
        s = bilibili._streams_from_playinfo(d)["原画"]
        self.assertTrue(s["url"].endswith(".flv?p=1&k=a"))

    def test_missing_extra_ok(self):
        d = self._data()
        del d["playurl_info"]["playurl"]["stream"][0]["format"][0]["codec"][0]["url_info"][0]["extra"]
        s = bilibili._streams_from_playinfo(d)["原画"]
        self.assertEqual(s["url"], "https://c1.bili.com/live/123.flv?p=1")

    def test_empty_playinfo(self):
        self.assertEqual(bilibili._streams_from_playinfo({}), {})

    def test_unknown_qn_uses_number_name(self):
        d = self._data()
        d["playurl_info"]["playurl"]["stream"][0]["format"][0]["codec"][0]["current_qn"] = 999
        self.assertIn("999", bilibili._streams_from_playinfo(d))


class TestBiliDispatch(unittest.TestCase):
    def test_get_site_routes_to_bilibili(self):
        self.assertIs(sites.get_site("https://live.bilibili.com/123456"), bilibili)

    def test_play_headers_has_referer(self):
        h = sites.play_headers("https://live.bilibili.com/123456")
        self.assertEqual(h["Referer"], "https://live.bilibili.com/")


class TestUnsupportedPlatform(unittest.TestCase):
    def test_get_site_raises(self):
        with self.assertRaises(RuntimeError):
            sites.get_site("https://www.example.com/123")

    def test_supported_lists_all_domains(self):
        for d in ("huya.com", "live.douyin.com", "douyu.com", "live.bilibili.com"):
            self.assertIn(d, sites.supported())


class TestQR(unittest.TestCase):
    """QR 生成器纯函数(标准正确性已用 OpenCV 解码器在开发期验证,此处守放置逻辑不回归)。"""

    def test_version_grows_with_length(self):
        v_short, _ = qr.encode_codewords("hi")
        v_long, _ = qr.encode_codewords("x" * 120)
        self.assertLessEqual(v_short, v_long)
        self.assertGreaterEqual(v_long, 6)

    def test_matrix_size(self):
        v, m = qr.make("https://live.bilibili.com/123456")
        self.assertEqual(len(m), v * 4 + 17)
        self.assertTrue(all(len(row) == len(m) for row in m))

    def test_finder_corners_dark(self):
        _, m = qr.make("hello")
        size = len(m)
        for r0, c0 in [(0, 0), (0, size - 7), (size - 7, 0)]:
            self.assertEqual(m[r0 + 3][c0 + 3], 1)      # finder 中心黑
            self.assertEqual(m[r0][c0], 1)              # 角黑

    def test_placement_roundtrip(self):
        # 按同一 fn/zigzag 逆读应还原码字流 → 放置逻辑自洽
        v, cw = qr.encode_codewords("https://passport.bilibili.com/x/abc?k=" + "v" * 40)
        m, fn = qr.build_matrix(v, cw)
        size = len(m)
        bits = ""
        right = size - 1
        while right > 0:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    col = right - j
                    up = ((right + 1) & 2) == 0
                    row = (size - 1 - vert) if up else vert
                    if not fn[row][col]:
                        bits += str(m[row][col])
            right -= 2
        read = [int(bits[i:i + 8], 2) for i in range(0, len(cw) * 8, 8)]
        self.assertEqual(read, cw)

    def test_render_dimensions(self):
        m = qr.make("hi")[1]
        out = qr.terminal_qr("hi")
        # 半块:每行含两行模块,行数 = ceil((size+2*quiet)/2)
        expected_rows = (len(m) + 8 + 1) // 2
        self.assertEqual(len(out.splitlines()), expected_rows)
        self.assertIn("█", out)

    def test_too_long_raises(self):
        with self.assertRaises(ValueError):
            qr.encode_codewords("x" * 300)


class TestBiliCookie(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_env_full_cookie_used_verbatim(self):
        os.environ["BILI_COOKIE"] = "SESSDATA=abc; bili_jct=xyz"
        self.assertEqual(bilibili._load_cookie(), "SESSDATA=abc; bili_jct=xyz")

    def test_env_bare_sessdata_wrapped(self):
        os.environ["BILI_COOKIE"] = "rawvalue"
        self.assertEqual(bilibili._load_cookie(), "SESSDATA=rawvalue")

    def test_cookies_from_setcookie_filters_wanted(self):
        setc = ["SESSDATA=aa; Path=/; HttpOnly", "bili_jct=bb; Path=/",
                "DedeUserID=123; Path=/", "buvid3=zzz; Path=/"]
        got = bilibili._cookies_from_setcookie(setc)
        self.assertIn("SESSDATA=aa", got)
        self.assertIn("bili_jct=bb", got)
        self.assertIn("DedeUserID=123", got)
        self.assertNotIn("buvid3", got)          # 非关键 cookie 不带

    def test_cookies_from_url(self):
        u = "https://passport.biligame.com/x/crossDomain?DedeUserID=1&SESSDATA=s%2Fx&bili_jct=j&gourl=x"
        got = bilibili._cookies_from_url(u)
        self.assertIn("SESSDATA=s/x", got)       # parse_qs 已解码
        self.assertIn("bili_jct=j", got)
        self.assertNotIn("gourl", got)

    def test_cookie_path_respects_xdg(self):
        os.environ["XDG_CONFIG_HOME"] = "/tmp/xdgcfg"
        p = bilibili._cookie_path()
        self.assertEqual(str(p), "/tmp/xdgcfg/iina-live/bilibili_cookie")

    def test_cookie_expiry_parsed(self):
        # SESSDATA 是 URL 编码的「创建戳,过期戳,签名」,取第二段
        sess = urllib.parse.quote("abc123,1750000000,def45*31")
        self.assertEqual(bilibili._cookie_expiry(f"SESSDATA={sess}; bili_jct=x"), 1750000000)

    def test_cookie_expiry_missing(self):
        self.assertIsNone(bilibili._cookie_expiry("bili_jct=x"))
        self.assertIsNone(bilibili._cookie_expiry(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
