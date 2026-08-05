#!/usr/bin/env python3
"""iina-series(点播/番剧)纯函数单元测试:标准库 unittest,零依赖、不触网。

覆盖:番剧地址解析、选集(集号/latest/正片优先/回退)、mp4/DASH 提流、派发路由。
触网部分(parse 的 HTTP)不在范围。

    python -m unittest tests.test_iina_series
"""
import argparse
import unittest

from iina_series import cli, sites
from iina_series.sites import bilibili as bgm


class TestResolveId(unittest.TestCase):
    def test_ep_and_ss(self):
        self.assertEqual(bgm.resolve_id("https://www.bilibili.com/bangumi/play/ep123456"), ("ep", 123456))
        self.assertEqual(bgm.resolve_id("https://www.bilibili.com/bangumi/play/ss28229"), ("ss", 28229))

    def test_non_bangumi_raises(self):
        with self.assertRaises(RuntimeError):
            bgm.resolve_id("https://www.bilibili.com/video/BV1xx")


class TestPickEpisode(unittest.TestCase):
    def test_ep_exact(self):
        season = {"episodes": [{"id": 1, "cid": 11}, {"id": 2, "cid": 22}]}
        self.assertEqual(bgm._pick_episode(season, "ep", 2)["cid"], 22)

    def test_ss_first(self):
        season = {"episodes": [{"id": 1, "cid": 11}, {"id": 2, "cid": 22}]}
        self.assertEqual(bgm._pick_episode(season, "ss", 28229)["cid"], 11)

    def test_by_number(self):
        season = {"episodes": [{"id": 1, "cid": 11}, {"id": 2, "cid": 22}, {"id": 3, "cid": 33}]}
        self.assertEqual(bgm._pick_episode(season, "ss", 0, episode=2)["cid"], 22)

    def test_by_title_number(self):
        # 列表位置≠正片集号时(混入重制版),按 ep.title 集号匹配优先
        season = {"episodes": [
            {"title": "1重制版", "cid": 1}, {"title": "1", "cid": 10},
            {"title": "2", "cid": 20}, {"title": "3", "cid": 30}]}
        self.assertEqual(bgm._pick_episode(season, "ss", 0, episode=2)["cid"], 20)

    def test_prefers_full_over_pv(self):
        # 同集号有 44s 看点 + 正片时,取时长最长(正片)
        season = {"episodes": [
            {"title": "185", "duration": 44000, "cid": 1},
            {"title": "185", "duration": 1241000, "cid": 2}]}
        self.assertEqual(bgm._pick_episode(season, "ss", 0, episode=185)["cid"], 2)

    def test_title_miss_falls_back_to_index(self):
        season = {"episodes": [{"title": "预告", "cid": 1}, {"title": "正片", "cid": 2}]}
        self.assertEqual(bgm._pick_episode(season, "ss", 0, episode=2)["cid"], 2)

    def test_number_overrides_ep_id(self):
        season = {"episodes": [{"id": 100, "cid": 11}, {"id": 200, "cid": 22}]}
        self.assertEqual(bgm._pick_episode(season, "ep", 100, episode=2)["cid"], 22)

    def test_out_of_range(self):
        season = {"episodes": [{"id": 1, "cid": 11}]}
        self.assertEqual(bgm._pick_episode(season, "ss", 0, episode=5), {})

    def test_latest(self):
        season = {"episodes": [{"id": 1, "cid": 11}, {"id": 2, "cid": 22}, {"id": 3, "cid": 33}]}
        self.assertEqual(bgm._pick_episode(season, "ss", 0, episode="latest")["cid"], 33)

    def test_empty(self):
        self.assertEqual(bgm._pick_episode({"episodes": []}, "ss", 1), {})


class TestStreams(unittest.TestCase):
    def test_from_play_mp4(self):
        play = {"quality": 80, "durl": [{"url": "http://x/v.mp4", "backup_url": ["http://y/v.mp4"]}]}
        s = bgm._streams_from_play(play)
        self.assertEqual(s["1080P"], {"quality": 80, "url": "http://x/v.mp4", "backups": ["http://y/v.mp4"]})

    def test_from_play_empty(self):
        self.assertEqual(bgm._streams_from_play({"durl": []}), {})

    def test_from_dash(self):
        dash = {
            "video": [
                {"id": 120, "codecs": "hev1", "baseUrl": "http://v/4k_h265", "backupUrl": ["http://v2/h265"]},
                {"id": 120, "codecs": "avc1", "baseUrl": "http://v/4k_h264", "backupUrl": ["http://v2/4k"]},
                {"id": 80, "codecs": "avc1", "baseUrl": "http://v/1080", "backup_url": []},
            ],
            "audio": [
                {"id": 30232, "bandwidth": 132000, "baseUrl": "http://a/mid"},
                {"id": 30280, "bandwidth": 192000, "baseUrl": "http://a/hi"},
            ],
        }
        s = bgm._streams_from_dash(dash)
        # 最高档 4K，同 qn 优先 H.264(avc1)，音轨取最高码率
        self.assertEqual(s["4K"]["quality"], 120)
        self.assertEqual(s["4K"]["url"], "http://v/4k_h264")
        self.assertEqual(s["4K"]["audio"], "http://a/hi")
        self.assertEqual(s["4K"]["backups"], ["http://v2/4k"])
        self.assertEqual(s["1080P"]["url"], "http://v/1080")

    def test_from_dash_empty(self):
        self.assertEqual(bgm._streams_from_dash({"video": [], "audio": []}), {})


class TestEpisodeArg(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(cli._episode_arg("latest"), "latest")
        self.assertEqual(cli._episode_arg("7"), 7)
        for bad in ("abc", "0", "-1"):
            with self.assertRaises(argparse.ArgumentTypeError):
                cli._episode_arg(bad)


class TestDispatch(unittest.TestCase):
    def test_routes_to_bilibili(self):
        self.assertIs(sites.get_site("https://www.bilibili.com/bangumi/play/ep1"), bgm)

    def test_play_headers_referer(self):
        self.assertEqual(sites.play_headers("https://www.bilibili.com/bangumi/play/ep1")["Referer"],
                         "https://www.bilibili.com/")

    def test_unsupported_raises(self):
        with self.assertRaises(RuntimeError):
            sites.get_site("https://www.huya.com/lpl")


if __name__ == "__main__":
    unittest.main(verbosity=2)
