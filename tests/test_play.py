"""play-with-mvp 根级 cli 统一命令入口测试。"""

import unittest
from unittest import mock

import cli


class RouteTests(unittest.TestCase):
    def test_routes_bilibili_series_url_automatically(self):
        target, args, prog = cli.route(
            [
                "https://www.bilibili.com/bangumi/play/ss28747",
                "--episode",
                "latest",
            ]
        )

        self.assertEqual(target, "series")
        self.assertEqual(args[0], "https://www.bilibili.com/bangumi/play/ss28747")
        self.assertEqual(prog, "cli")

    def test_live_subcommand_forces_live_and_is_removed(self):
        target, args, prog = cli.route(["live", "https://www.huya.com/lpl"])

        self.assertEqual(target, "live")
        self.assertEqual(args, ["https://www.huya.com/lpl"])
        self.assertEqual(prog, "cli live")

    def test_series_subcommand_forces_series_and_is_removed(self):
        target, args, prog = cli.route(["series", "https://b23.tv/example"])

        self.assertEqual(target, "series")
        self.assertEqual(args, ["https://b23.tv/example"])
        self.assertEqual(prog, "cli series")

    def test_non_series_arguments_default_to_live(self):
        target, args, prog = cli.route(["--mode", "serve-only"])

        self.assertEqual(target, "live")
        self.assertEqual(args, ["--mode", "serve-only"])
        self.assertEqual(prog, "cli")

    def test_unrelated_url_with_series_like_path_stays_live(self):
        target, _, _ = cli.route(["https://example.com/bangumi/play/demo"])

        self.assertEqual(target, "live")

    def test_option_value_does_not_override_live_url_routing(self):
        target, _, _ = cli.route(
            [
                "--title",
                "https://www.bilibili.com/bangumi/play/ss28747",
                "https://www.huya.com/lpl",
            ]
        )

        self.assertEqual(target, "live")

    def test_main_dispatches_to_series_cli(self):
        url = "https://www.bilibili.com/bangumi/play/ep285395"
        with mock.patch.object(cli.series_cli, "main", return_value=7) as main:
            result = cli.main([url, "--print"])

        self.assertEqual(result, 7)
        main.assert_called_once_with([url, "--print"], prog="cli")


if __name__ == "__main__":
    unittest.main(verbosity=2)
