#!/usr/bin/env python3
"""tests/test_archive_path.py — archive_path() のユニットテスト"""
import sys
import os
import unittest

# スクリプトパスを追加
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from crawl_archive import archive_path  # noqa: E402


class TestArchivePath(unittest.TestCase):
    def test_retty_me_area(self):
        """retty.me のエリアパスが正しく変換される"""
        result = archive_path("retty.me", "/area/PRE13/")
        self.assertEqual(result, "me/retty.me/area/PRE13/index.html")

    def test_shiroi_chiba(self):
        """白井市の自治体 FQDN が正しく変換される"""
        result = archive_path("www.city.shiroi.chiba.jp", "/soshiki/norin/")
        self.assertEqual(
            result,
            "jp/chiba.jp/shiroi.chiba.jp/city.shiroi.chiba.jp/www.city.shiroi.chiba.jp/soshiki/norin/index.html",
        )

    def test_root_path(self):
        """URL パスが空（ルート）の場合は index.html になる"""
        result = archive_path("retty.me", "/")
        self.assertEqual(result, "me/retty.me/index.html")

    def test_empty_path(self):
        """URL パスが空文字の場合も index.html になる"""
        result = archive_path("retty.me", "")
        self.assertEqual(result, "me/retty.me/index.html")

    def test_path_with_html(self):
        """.html 拡張子付きパスはそのまま"""
        result = archive_path("retty.me", "/page.html")
        self.assertEqual(result, "me/retty.me/page.html")

    def test_two_label_domain(self):
        """2 ラベルドメインの逆 DNS 変換"""
        result = archive_path("example.com", "/foo/bar/")
        self.assertEqual(result, "com/example.com/foo/bar/index.html")


if __name__ == "__main__":
    unittest.main()
