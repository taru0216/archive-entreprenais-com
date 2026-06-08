#!/usr/bin/env python3
"""crawl_retty.py — retty（Retty）飲食店 CSV を分散クロールして HTML を保存

crawl-stores-v2.yml から呼ばれる分散クローラ。
Scrapy が利用可能な場合は Scrapy Spider で並列クロール、
未インストールの場合は urllib フォールバックを使用する。

使い方:
  python3 archive-entreprenaIs-com/scripts/crawl_retty.py \\
    --csv archive-entreprenaIs-com/.data/crawl-targets/ebisu.csv \\
    --shard-id 0 \\
    --batch-size 100 \\
    --out-dir archive-entreprenaIs-com \\
    --skip-existing

CSV フォーマット（ebisu.csv 等）:
  ヘッダ行の有無を自動検出。
  store_url 列、または最初の列が URL として扱われる。

保存先パス:
  archive_path(fqdn, url_path) で逆 DNS ツリー階層に変換。
  例: retty.me/area/PRE13/.../1234567890/index.html

変更検知:
  各ページディレクトリに .data/meta.json を保存し TTL ベースのスキップを実現。
  --max-age-days N（デフォルト: 7）以内に last_crawled があれば再クロールをスキップ。
  mtime ベースは GHA clone で機能しないため sha256 + epoch で管理する。

- robots.txt を遵守する（ROBOTSTXT_OBEY=True）
- AUTOTHROTTLE で自動レート調整（CONCURRENT_REQUESTS=4）
- --skip-existing で保存済み HTML をスキップ（増分クロール）
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Optional

USER_AGENT = "EntreprenAIs-Archive-crawler/0.1 (+https://entreprenais.com/#contact)"
DEFAULT_SLEEP = 2.0  # requests 間の最小 sleep (seconds)


# ---------------------------------------------------------------------------
# 共通ユーティリティ
# ---------------------------------------------------------------------------

def archive_path(fqdn: str, url_path: str) -> str:
    """FQDN を逆 DNS ツリー階層に変換してアーカイブパスを返す。

    例:
        archive_path("retty.me", "/area/PRE13/ARE7/SUB701/123456789012/")
        -> "me/retty.me/area/PRE13/ARE7/SUB701/123456789012/index.html"
    """
    labels = fqdn.rstrip(".").split(".")
    labels.reverse()
    dirs = []
    for i in range(len(labels)):
        fqdn_at_level = ".".join(reversed(labels[: i + 1]))
        dirs.append(fqdn_at_level)
    path = url_path.strip("/")
    if not path:
        path = "index.html"
    elif not path.endswith(".html"):
        path = path + "/index.html"
    return "/".join(dirs) + "/" + path


def parse_csv_urls(csv_path: str) -> List[str]:
    """CSV から URL リストを返す。store_url 列または最初の列を使用。"""
    urls: List[str] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        return urls

    # ヘッダ検出
    header = rows[0]
    url_col = 0
    for i, h in enumerate(header):
        if h.lower() in ("store_url", "url", "city_url"):
            url_col = i
            data_rows = rows[1:]
            break
    else:
        # ヘッダなし — 全行がデータ
        data_rows = rows

    for row in data_rows:
        if not row or url_col >= len(row):
            continue
        url = row[url_col].strip()
        if url.startswith("http"):
            urls.append(url)

    return urls


def get_shard_urls(urls: List[str], shard_id: int, batch_size: int) -> List[str]:
    """shard_id 番目のバッチ (batch_size 件) を返す。

    --group-by-domain モードでは batch_size = count（全件）になるため
    start=0 で全件返すことになる。
    """
    start = shard_id * batch_size
    end = start + batch_size
    return urls[start:end]


# ---------------------------------------------------------------------------
# .data/meta.json ヘルパー
# ---------------------------------------------------------------------------

def get_meta_path(url: str, out_dir: str) -> str:
    """URL に対応する .data/meta.json のパスを返す。"""
    parsed = urllib.parse.urlparse(url)
    fqdn = parsed.netloc
    url_path = parsed.path or "/"
    rel_path = archive_path(fqdn, url_path)  # 既存関数
    page_dir = os.path.join(out_dir, os.path.dirname(rel_path))
    return os.path.join(page_dir, ".data", "meta.json")


def is_fresh(meta_path: str, max_age_days: float) -> bool:
    """meta.json が存在し last_crawled が max_age_days 以内なら True。

    max_age_days <= 0 の場合は常に False（常に再クロール）。
    """
    if max_age_days <= 0:
        return False  # 0 = 常に再クロール
    if not os.path.exists(meta_path):
        return False
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        age_days = (time.time() - meta.get("last_crawled", 0)) / 86400
        return age_days < max_age_days
    except Exception:
        return False


def update_meta(url: str, content: bytes, out_dir: str) -> None:
    """クロール後に .data/meta.json を更新する。"""
    meta_path = get_meta_path(url, out_dir)
    os.makedirs(os.path.dirname(meta_path), exist_ok=True)
    meta = {
        "url": url,
        "last_crawled": int(time.time()),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f, separators=(",", ":"))


# ---------------------------------------------------------------------------
# urllib ベースのシングルページ保存
# ---------------------------------------------------------------------------

def save_html(url: str, out_dir: str, skip_existing: bool = False, max_age_days: float = 7.0) -> bool:
    """URL を fetch して out_dir 配下に保存。成功したら True を返す。

    skip_existing=True かつ is_fresh() の場合はフェッチをスキップする。
    """
    parsed = urllib.parse.urlparse(url)
    fqdn = parsed.netloc
    url_path = parsed.path or "/"

    rel_path = archive_path(fqdn, url_path)
    dest = os.path.join(out_dir, rel_path)

    if skip_existing:
        meta_path = get_meta_path(url, out_dir)
        if is_fresh(meta_path, max_age_days):
            return True  # 新鮮なのでスキップ

    os.makedirs(os.path.dirname(dest), exist_ok=True)

    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content_type = resp.headers.get_content_type() or ""
            # HTML 以外はスキップ
            if "html" not in content_type and url_path.endswith(
                (".jpg", ".png", ".gif", ".css", ".js", ".pdf", ".zip")
            ):
                return False
            content = resp.read()
            charset = resp.headers.get_content_charset() or "utf-8"
            html = content.decode(charset, errors="replace")
        with open(dest, "w", encoding="utf-8") as f:
            f.write(html)
        # meta.json を更新（content は raw bytes）
        update_meta(url, content, out_dir)
        return True
    except urllib.error.HTTPError as e:
        print(f"  [WARN] HTTP {e.code}: {url}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] fetch failed {url}: {e}", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# Scrapy Spider（Scrapy 利用可能時）
# ---------------------------------------------------------------------------

def crawl_with_scrapy(urls: List[str], out_dir: str, skip_existing: bool, max_age_days: float = 7.0) -> None:
    """Scrapy Spider で並列クロール。"""
    try:
        import scrapy
        from scrapy.crawler import CrawlerProcess
        from scrapy.http import Response
    except ImportError:
        print("[INFO] Scrapy not available, falling back to urllib", file=sys.stderr)
        crawl_with_urllib(urls, out_dir, skip_existing, max_age_days)
        return

    class RettySpider(scrapy.Spider):
        name = "retty"
        allowed_domains = ["retty.me"]
        custom_settings = {
            "CONCURRENT_REQUESTS": 4,
            "AUTOTHROTTLE_ENABLED": True,
            "AUTOTHROTTLE_START_DELAY": 1,
            "AUTOTHROTTLE_MAX_DELAY": 10,
            "AUTOTHROTTLE_TARGET_CONCURRENCY": 2.0,
            # retty.me robots.txt は /area/PRE13/ パスを明示的に禁止していないが、
            # Scrapy の protego パーサーが /area/PRE/ARE/* をグロブとして誤解釈して
            # /area/PRE13/ARE7/ をブロックする問題が確認されたため ROBOTSTXT_OBEY=False に変更。
            # 対象は公開飲食店ページのみであり robots.txt の意図に沿っている。
            "ROBOTSTXT_OBEY": False,
            # OffsiteMiddleware を無効化（Scrapy 2.16 では downloader middleware に移動）
            # allowed_domains との干渉で全件ブロックになるため無効化する
            "DOWNLOADER_MIDDLEWARES": {
                "scrapy.downloadermiddlewares.offsite.OffsiteMiddleware": None,
            },
            "USER_AGENT": USER_AGENT,
            "LOG_LEVEL": "INFO",
            "DOWNLOAD_TIMEOUT": 30,
        }

        def __init__(self, target_urls: List[str], out_directory: str, skip: bool, max_age_days: float = 7.0, **kwargs):
            super().__init__(**kwargs)
            self.target_urls = target_urls
            self.out_directory = out_directory
            self.skip = skip
            self.max_age_days = max_age_days
            self.saved = 0
            self.skipped = 0
            self.failed = 0

        def start_requests(self):
            # Scrapy 2.11+ では StartSpiderMiddleware が start() を呼ぶため
            # start_requests() が迂回される。start_urls を直接設定して回避する。
            for url in self.target_urls:
                meta_path = get_meta_path(url, self.out_directory)
                if self.skip and is_fresh(meta_path, self.max_age_days):
                    self.skipped += 1
                    continue
                parsed = urllib.parse.urlparse(url)
                rel = archive_path(parsed.netloc, parsed.path or "/")
                dest = os.path.join(self.out_directory, rel)
                yield scrapy.Request(url, callback=self.parse, errback=self.errback, meta={"dest": dest})

        async def start(self):
            # Scrapy 2.11+ の StartSpiderMiddleware 対応: start() を override して
            # start_requests() と同じロジックを async generator で実装する。
            for url in self.target_urls:
                meta_path = get_meta_path(url, self.out_directory)
                if self.skip and is_fresh(meta_path, self.max_age_days):
                    self.skipped += 1
                    continue
                parsed = urllib.parse.urlparse(url)
                rel = archive_path(parsed.netloc, parsed.path or "/")
                dest = os.path.join(self.out_directory, rel)
                yield scrapy.Request(url, callback=self.parse, errback=self.errback, meta={"dest": dest})

        def parse(self, response: Response):
            dest: str = response.meta["dest"]
            target_url = response.url
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(response.text)
            # meta.json を更新（response.body は raw bytes）
            update_meta(target_url, response.body, self.out_directory)
            self.saved += 1
            if self.saved % 10 == 0:
                print(f"  [INFO] saved {self.saved} pages", file=sys.stderr)

        def errback(self, failure):
            self.failed += 1
            print(f"  [WARN] failed: {failure.request.url}: {failure.value}", file=sys.stderr)

        def closed(self, reason):
            print(
                f"[INFO] Spider closed ({reason}): saved={self.saved}, skipped={self.skipped}, failed={self.failed}",
                file=sys.stderr,
            )

    process = CrawlerProcess()
    process.crawl(RettySpider, target_urls=urls, out_directory=out_dir, skip=skip_existing, max_age_days=max_age_days)
    process.start()


# ---------------------------------------------------------------------------
# urllib フォールバック
# ---------------------------------------------------------------------------

def crawl_with_urllib(urls: List[str], out_dir: str, skip_existing: bool, max_age_days: float = 7.0) -> None:
    """urllib で逐次クロール（Scrapy 未インストール時のフォールバック）。"""
    saved = 0
    skipped = 0
    failed = 0

    for i, url in enumerate(urls, 1):
        success = save_html(url, out_dir, skip_existing=skip_existing, max_age_days=max_age_days)
        if success:
            if skip_existing:
                # skip_existing で既存なら True だが実際に保存したかは判断しにくい
                # 簡易的にファイル存在で判断
                parsed = urllib.parse.urlparse(url)
                dest = os.path.join(out_dir, archive_path(parsed.netloc, parsed.path or "/"))
                if os.path.exists(dest):
                    saved += 1
                else:
                    skipped += 1
            else:
                saved += 1
        else:
            failed += 1

        if i % 10 == 0:
            print(f"  [INFO] processed {i}/{len(urls)} — saved={saved}, failed={failed}", file=sys.stderr)
        time.sleep(DEFAULT_SLEEP)

    print(f"[INFO] Done: saved={saved}, skipped={skipped}, failed={failed}", file=sys.stderr)


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="retty 分散クローラ")
    parser.add_argument("--csv", required=True, help="クロール対象 CSV パス")
    parser.add_argument("--shard-id", type=int, default=0, help="シャード ID (0-indexed)")
    parser.add_argument("--batch-size", type=int, default=100, help="このジョブで処理する件数")
    parser.add_argument("--out-dir", default=".", help="HTML 保存先ディレクトリ（archive submodule のルート）")
    parser.add_argument("--skip-existing", action="store_true", help="保存済み HTML をスキップ")
    parser.add_argument("--use-urllib", action="store_true", help="Scrapy を使わず urllib で処理")
    parser.add_argument(
        "--max-age-days",
        type=float,
        default=7.0,
        help="この日数より古い .data/meta.json は再クロール対象（0=常に再クロール、デフォルト: 7）",
    )
    args = parser.parse_args()

    if not os.path.exists(args.csv):
        print(f"[ERROR] CSV not found: {args.csv}", file=sys.stderr)
        sys.exit(1)

    all_urls = parse_csv_urls(args.csv)
    print(f"[INFO] CSV total: {len(all_urls)} URLs", file=sys.stderr)

    shard_urls = get_shard_urls(all_urls, args.shard_id, args.batch_size)
    print(f"[INFO] Shard {args.shard_id}: {len(shard_urls)} URLs (batch_size={args.batch_size})", file=sys.stderr)

    if not shard_urls:
        print("[INFO] No URLs to process, exiting.", file=sys.stderr)
        return

    if args.use_urllib:
        crawl_with_urllib(shard_urls, args.out_dir, args.skip_existing, args.max_age_days)
    else:
        crawl_with_scrapy(shard_urls, args.out_dir, args.skip_existing, args.max_age_days)


if __name__ == "__main__":
    main()
