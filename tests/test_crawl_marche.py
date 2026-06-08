#!/usr/bin/env python3
"""tests/test_crawl_marche.py — crawl_marche.py のユニットテスト"""
import sys
import os
import csv
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from crawl_marche import is_marche_url, is_same_domain, gen_csv_main  # noqa: E402


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


class TestGenCsvDryRun(unittest.TestCase):
    def test_gen_csv_dry_run(self):
        """dry-run モードで gen-csv が実行できること（外部通信なし）"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            writer = csv.writer(f)
            writer.writerow(["city_url", "city_slug", "pref"])
            writer.writerow(["https://www.city.shiroi.chiba.jp/", "shiroi", "chiba"])
            target_csv = f.name

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8"
        ) as f:
            out_csv = f.name

        try:
            result = gen_csv_main(
                [
                    "--target-csv", target_csv,
                    "--out", out_csv,
                    "--dry-run",
                ]
            )
            # dry-run では外部通信なしで正常終了（0 ページなので 0 を返す）
            self.assertEqual(result, 0)

            # 出力 CSV が作成されていること
            self.assertTrue(os.path.exists(out_csv))

            with open(out_csv, newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
            # dry-run では 0 件（外部通信なし）
            self.assertEqual(len(rows), 0)
        finally:
            os.unlink(target_csv)
            os.unlink(out_csv)


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
        slugs = [r.get("city_slug", "") for r in rows]
        self.assertIn("shiroi", slugs, "白井市エントリが存在しない")


if __name__ == "__main__":
    unittest.main()
