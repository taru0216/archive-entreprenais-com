#!/usr/bin/env python3
"""tests/test_crawl_marche.py — crawl_cities.py のユニットテスト（後方互換）

NOTE: crawl_marche.py は crawl_cities.py にリネームされました。
このファイルは後方互換のため残していますが、新しいテストは test_city_spider.py を参照してください。
"""
import sys
import os
import csv
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from crawl_cities import is_marche_url, is_same_domain, load_targets  # noqa: E402


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


class TestLoadTargetsDryRun(unittest.TestCase):
    def test_load_targets_with_domain_filter(self):
        """load_targets が target_domain でフィルタリングできること（外部通信なし）"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            writer = csv.writer(f)
            writer.writerow(["city_url", "city_slug", "pref"])
            writer.writerow(["https://www.city.shiroi.chiba.jp/sangyo/", "shiroi", "chiba"])
            writer.writerow(["https://www.city.matsudo.chiba.jp/jigyosya/", "matsudo", "chiba"])
            target_csv = f.name

        try:
            # フィルタあり: shiroi のみ
            targets = load_targets(target_csv, target_domain="www.city.shiroi.chiba.jp")
            self.assertEqual(len(targets), 1)
            self.assertEqual(targets[0][0], "shiroi")

            # フィルタなし: 全件
            all_targets = load_targets(target_csv)
            self.assertEqual(len(all_targets), 2)
        finally:
            os.unlink(target_csv)


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
