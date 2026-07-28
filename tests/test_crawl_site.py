#!/usr/bin/env python3
"""tests/test_crawl_site.py — crawl_site.py のユニットテスト（ネットワークなし）"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from crawl_site import (  # noqa: E402
    extract_title_and_text,
    parse_sitemap_locs,
    same_domain,
    site_rel_from_out_dir,
    url_to_relpath,
    write_sitemap,
)


class TestParseSitemapLocs(unittest.TestCase):
    def test_extracts_locs_in_order_dedup(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://example.com/</loc></url>"
            "<url><loc>https://example.com/qa</loc></url>"
            "<url><loc>https://example.com/qa</loc></url>"
            "</urlset>"
        )
        self.assertEqual(
            parse_sitemap_locs(xml),
            ["https://example.com/", "https://example.com/qa"],
        )

    def test_empty_sitemap(self):
        self.assertEqual(parse_sitemap_locs("<urlset></urlset>"), [])


class TestSameDomainAndExtFilter(unittest.TestCase):
    def test_same_domain_true(self):
        self.assertTrue(same_domain("https://www.miyarail.co.jp/qa", "www.miyarail.co.jp"))

    def test_same_domain_false(self):
        self.assertFalse(same_domain("https://other.example.com/qa", "www.miyarail.co.jp"))

    def test_same_domain_case_insensitive(self):
        self.assertTrue(same_domain("https://WWW.MIYARAIL.CO.JP/qa", "www.miyarail.co.jp"))


class TestUrlToRelpath(unittest.TestCase):
    def test_root(self):
        self.assertEqual(url_to_relpath("https://www.miyarail.co.jp/"), ".")

    def test_root_no_trailing_slash(self):
        self.assertEqual(url_to_relpath("https://www.miyarail.co.jp"), ".")

    def test_simple_path(self):
        self.assertEqual(url_to_relpath("https://www.miyarail.co.jp/qa"), "qa")

    def test_strips_query_and_fragment(self):
        self.assertEqual(
            url_to_relpath("https://www.miyarail.co.jp/qa?tab=fare#section"), "qa"
        )


class TestExtractTitleAndText(unittest.TestCase):
    def test_extracts_title_and_body_skips_boilerplate(self):
        html = """
        <html><head><title> よくある質問 | 宇都宮ライトレール </title>
        <style>.x{color:red}</style></head>
        <body>
        <header>ヘッダーメニュー</header>
        <nav>ナビゲーション</nav>
        <script>console.log('x')</script>
        <main>運賃は対距離制です。詳しくはこちら。</main>
        <footer>フッターリンク</footer>
        </body></html>
        """
        title, text = extract_title_and_text(html)
        self.assertEqual(title, "よくある質問 | 宇都宮ライトレール")
        self.assertIn("運賃は対距離制です", text)
        self.assertNotIn("ヘッダーメニュー", text)
        self.assertNotIn("ナビゲーション", text)
        self.assertNotIn("console.log", text)
        self.assertNotIn("フッターリンク", text)

    def test_collapses_whitespace(self):
        html = "<html><body><p>a</p>\n\n  <p>b</p></body></html>"
        _, text = extract_title_and_text(html)
        self.assertEqual(text, "a b")

    def test_skips_div_based_menu_by_class_name(self):
        """<nav>を使わずdivのclass名だけでメニューを実装しているテーマ（実サイトで
        確認済みのパターン）でも、汎用的な命名慣習（menu/drawer等）で除外できること。"""
        html = """
        <html><body>
        <div class="drawer-menu-wrapper"><div class="menu_contents">
        ホーム ご利用案内 よくあるご質問
        </div></div>
        <main>運賃は対距離制です。</main>
        <div class="footer_menu">会社概要 採用情報</div>
        </body></html>
        """
        _, text = extract_title_and_text(html)
        self.assertIn("運賃は対距離制です", text)
        self.assertNotIn("ホーム", text)
        self.assertNotIn("会社概要", text)

    def test_void_tags_do_not_desync_skip_stack(self):
        """imgのような閉じタグの無い要素があっても、その後のskip判定がずれないこと。"""
        html = """
        <html><body>
        <nav>メニュー<img src="x.png">続き</nav>
        <main>本文です。</main>
        </body></html>
        """
        _, text = extract_title_and_text(html)
        self.assertIn("本文です", text)
        self.assertNotIn("メニュー", text)
        self.assertNotIn("続き", text)


class TestSiteRelFromOutDir(unittest.TestCase):
    def test_docs_prefixed(self):
        self.assertEqual(site_rel_from_out_dir("docs/miyarail.co.jp"), "miyarail.co.jp")

    def test_requires_docs_prefix(self):
        with self.assertRaises(ValueError):
            site_rel_from_out_dir("output/miyarail.co.jp")


class TestWriteSitemap(unittest.TestCase):
    def test_writes_parseable_sitemap(self):
        import shutil
        import tempfile

        from crawl_site import parse_sitemap_locs

        tmp = tempfile.mkdtemp()
        try:
            urls = [
                "https://taru0216.github.io/archive-entreprenais-com/miyarail.co.jp/content.json",
                "https://taru0216.github.io/archive-entreprenais-com/miyarail.co.jp/qa/content.json",
            ]
            write_sitemap(tmp, urls)
            out_path = os.path.join(tmp, "sitemap.xml")
            self.assertTrue(os.path.exists(out_path))
            with open(out_path, encoding="utf-8") as f:
                xml = f.read()
            self.assertEqual(parse_sitemap_locs(xml), urls)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
