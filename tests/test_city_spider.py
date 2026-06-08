#!/usr/bin/env python3
"""tests/test_city_spider.py — crawl_cities.py (CitySpider 1ステップ版) のユニットテスト"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from crawl_cities import (  # noqa: E402
    is_marche_url,
    is_same_domain,
    is_fresh,
    get_meta_path,
    update_meta,
    load_targets,
    main,
)


class TestIsMarcheUrl(unittest.TestCase):
    def test_norin_keyword(self):
        self.assertTrue(is_marche_url("https://www.city.shiroi.chiba.jp/soshiki/norin/"))

    def test_nougyo_keyword(self):
        self.assertTrue(is_marche_url("https://example.jp/nougyo/jigyou/"))

    def test_marche_keyword(self):
        self.assertTrue(is_marche_url("https://example.jp/marche/2024/"))

    def test_event_keyword(self):
        self.assertTrue(is_marche_url("https://example.jp/event/harvest/"))

    def test_not_marche_url(self):
        self.assertFalse(is_marche_url("https://example.jp/gyosei/somu/"))

    def test_agri_keyword(self):
        self.assertTrue(is_marche_url("https://example.jp/agri/"))


class TestIsSameDomain(unittest.TestCase):
    def test_same_domain(self):
        self.assertTrue(
            is_same_domain(
                "https://www.city.shiroi.chiba.jp/soshiki/norin/",
                "www.city.shiroi.chiba.jp",
            )
        )

    def test_different_domain(self):
        self.assertFalse(
            is_same_domain("https://www.google.com/", "www.city.shiroi.chiba.jp")
        )


class TestIsFresh(unittest.TestCase):
    def test_fresh_meta(self):
        """max_age_days 以内の meta.json は fresh と判定されること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            meta_path = os.path.join(tmpdir, ".data", "meta.json")
            os.makedirs(os.path.dirname(meta_path))
            with open(meta_path, "w") as f:
                json.dump({"last_crawled": int(time.time()), "url": "https://x.jp/"}, f)
            self.assertTrue(is_fresh(meta_path, max_age_days=7.0))

    def test_stale_meta(self):
        """max_age_days より古い meta.json は stale と判定されること"""
        with tempfile.TemporaryDirectory() as tmpdir:
            meta_path = os.path.join(tmpdir, ".data", "meta.json")
            os.makedirs(os.path.dirname(meta_path))
            old_ts = int(time.time()) - int(8 * 86400)
            with open(meta_path, "w") as f:
                json.dump({"last_crawled": old_ts, "url": "https://x.jp/"}, f)
            self.assertFalse(is_fresh(meta_path, max_age_days=7.0))

    def test_missing_meta(self):
        """meta.json が存在しない場合は False"""
        self.assertFalse(is_fresh("/tmp/nonexistent/.data/meta.json", max_age_days=7.0))

    def test_zero_max_age(self):
        """max_age_days=0 の場合は常に False"""
        with tempfile.TemporaryDirectory() as tmpdir:
            meta_path = os.path.join(tmpdir, ".data", "meta.json")
            os.makedirs(os.path.dirname(meta_path))
            with open(meta_path, "w") as f:
                json.dump({"last_crawled": int(time.time()), "url": "https://x.jp/"}, f)
            self.assertFalse(is_fresh(meta_path, max_age_days=0))


class TestLoadTargets(unittest.TestCase):
    def test_load_city_url_format(self):
        """city_url/city_slug ヘッダ形式を読み込めること"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            writer = csv.writer(f)
            writer.writerow(["city_url", "city_slug", "pref"])
            writer.writerow(["https://www.city.shiroi.chiba.jp/sangyo/nogyo/", "shiroi", "chiba"])
            target_csv = f.name
        try:
            targets = load_targets(target_csv)
            self.assertEqual(len(targets), 1)
            slug, url = targets[0]
            self.assertEqual(slug, "shiroi")
            self.assertEqual(url, "https://www.city.shiroi.chiba.jp/sangyo/nogyo/")
        finally:
            os.unlink(target_csv)

    def test_load_slug_url_format(self):
        """slug/url ヘッダ形式も読み込めること（marche-targets-chiba.csv 形式）"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            writer = csv.writer(f)
            writer.writerow(["slug", "url", "pref", "display_name"])
            writer.writerow(["shiroi", "https://www.city.shiroi.chiba.jp/sangyo/nogyo/", "千葉県", "白井市"])
            target_csv = f.name
        try:
            targets = load_targets(target_csv)
            self.assertEqual(len(targets), 1)
            slug, url = targets[0]
            self.assertEqual(slug, "shiroi")
            self.assertEqual(url, "https://www.city.shiroi.chiba.jp/sangyo/nogyo/")
        finally:
            os.unlink(target_csv)

    def test_filter_by_domain(self):
        """target_domain 指定時は対象ドメインのみフィルタリングされること"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            writer = csv.writer(f)
            writer.writerow(["city_url", "city_slug", "pref"])
            writer.writerow(["https://www.city.shiroi.chiba.jp/sangyo/", "shiroi", "chiba"])
            writer.writerow(["https://www.city.matsudo.chiba.jp/jigyosya/", "matsudo", "chiba"])
            target_csv = f.name
        try:
            targets = load_targets(target_csv, target_domain="www.city.shiroi.chiba.jp")
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0][0], "shiroi")
        finally:
            os.unlink(target_csv)


class TestMainCli(unittest.TestCase):
    def test_dry_run_exits_zero(self):
        """--dry-run モードでは外部通信なしに正常終了すること"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            writer = csv.writer(f)
            writer.writerow(["city_url", "city_slug", "pref"])
            writer.writerow(["https://www.city.shiroi.chiba.jp/sangyo/nogyo/", "shiroi", "chiba"])
            target_csv = f.name
        try:
            # main() が SystemExit を raise しないこと
            import io
            from contextlib import redirect_stderr
            buf = io.StringIO()
            with self.assertRaises(SystemExit) as ctx:
                with redirect_stderr(buf):
                    sys.argv = [
                        "crawl_cities.py",
                        "--target-csv", target_csv,
                        "--target-domain", "www.city.shiroi.chiba.jp",
                        "--dry-run",
                    ]
                    main()
            self.assertEqual(ctx.exception.code, 0)
        finally:
            os.unlink(target_csv)

    def test_no_args_exits_nonzero(self):
        """引数なしでは非ゼロ終了すること"""
        with self.assertRaises(SystemExit) as ctx:
            sys.argv = ["crawl_cities.py"]
            main()
        self.assertNotEqual(ctx.exception.code, 0)

    def test_missing_target_csv_exits_nonzero(self):
        """存在しない --target-csv を指定した場合は非ゼロ終了すること"""
        with self.assertRaises(SystemExit) as ctx:
            sys.argv = [
                "crawl_cities.py",
                "--target-csv", "/tmp/nonexistent_file_abc123.csv",
                "--target-domain", "www.city.shiroi.chiba.jp",
            ]
            main()
        self.assertNotEqual(ctx.exception.code, 0)


class TestTargetsCsvFormat(unittest.TestCase):
    def test_marche_targets_csv_exists(self):
        """marche-targets.csv が存在し、白井市エントリが含まれること"""
        csv_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            ".data",
            "crawl-targets",
            "marche-targets.csv",
        )
        self.assertTrue(os.path.exists(csv_path), f"CSV not found: {csv_path}")

        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        self.assertGreater(len(rows), 0, "marche-targets.csv が空")
        # city_slug または slug 列で白井市を探す
        slugs = [
            (r.get("city_slug") or r.get("slug") or "") for r in rows
        ]
        self.assertIn("shiroi", slugs, "白井市エントリが存在しない")


if __name__ == "__main__":
    unittest.main()
