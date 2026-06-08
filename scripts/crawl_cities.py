#!/usr/bin/env python3
"""crawl_cities.py — 自治体公式サイトの農業・産業関連ページの生 HTML アーカイブ

自治体（cities）の農業・産業情報をクロールして保存するオープンデータ収集スクリプト。
収集した cities データは marche ページの build 等、複数の用途に利用される。

2 ステップ構成:
  gen-csv  : 自治体トップページから農業・イベントページ URL を探索して CSV 出力
  save-html: CSV から各ページを archive_path() 規則で保存（Scrapy 並列クロール）

使い方:
  python3 scripts/crawl_cities.py gen-csv \\
    --target-csv .data/crawl-targets/marche-targets.csv \\
    --out .data/crawl-targets/marche-pages.csv \\
    --depth 1 --sleep 3

  python3 scripts/crawl_cities.py save-html \\
    --csv .data/crawl-targets/marche-pages.csv \\
    --sleep 3

  # ドライラン（外部通信なし・URL 生成のみ確認）
  python3 scripts/crawl_cities.py gen-csv \\
    --target-csv .data/crawl-targets/marche-targets.csv \\
    --out .data/crawl-targets/marche-pages.csv \\
    --dry-run

CSV フォーマット（marche-targets.csv / marche-targets-chiba.csv 等）:
  ヘッダは city_url/city_slug/pref（旧形式）または url/slug/pref（新形式）の両方に対応。
  display_name 列は省略可能。
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
import urllib.request
import urllib.error
import urllib.robotparser
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

# crawl_archive モジュールから archive_path をインポート
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from crawl_archive import archive_path  # noqa: E402

USER_AGENT = "EntreprenAIs-Archive-crawler/0.1 (+https://entreprenais.com/#contact)"

# 農業・マルシェ・イベント関連キーワード（URL パスまたはページテキストに含まれる場合に対象とする）
# 自治体サイトの農業関連 URL パターンを網羅的にカバーする
MARCHE_KEYWORDS = [
    # 日本語
    "農業", "農林", "産地", "直売", "マルシェ", "農産", "地産", "収穫", "栽培",
    "イベント", "祭", "産業祭", "収穫祭",
    # ローマ字 / 英語（自治体 URL で使われる主要パターン）
    "norin",       # 農林（農林水産 / 農林業）
    "nougyo",      # 農業（のうぎょう）
    "nogyo",       # 農業（短縮形）
    "nousuisan",   # 農水産（船橋市など）
    "chokubai",    # 直売
    "marche",      # マルシェ
    "event",       # イベント
    "agri",        # agriculture
    "farm",        # farm
    "sangyo",      # 産業（sangyo + 農業 URL に多い）
    "kanko",       # 観光（道の駅など）
    "kankou",      # 観光
    "chiisan",     # 地産地消
    "chisanch",    # 地産地消
]

# robots.txt キャッシュ
_robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}

# バイナリとみなす URL 拡張子（事前スキップ対象）
_BINARY_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".gz", ".tar", ".rar", ".7z",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".flv",
    ".csv", ".tsv",
    ".exe", ".dmg", ".pkg", ".deb", ".rpm",
)


def is_binary_url(url: str) -> bool:
    """URL のパス末尾がバイナリ拡張子かどうかを判定する。"""
    path = urlparse(url).path.lower()
    return path.endswith(_BINARY_EXTENSIONS)


def http_get(url: str, timeout: int = 30) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get_content_type() or ""
            # text/html 以外（PDF・画像・バイナリ等）はリンク探索対象外
            if not content_type.startswith("text/html"):
                return None
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  [WARN] HTTP {e.code} for {url}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] fetch failed {url}: {e}", file=sys.stderr)
    return None


def is_allowed_by_robots(url: str) -> bool:
    """robots.txt の Disallow をチェックする。"""
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    if base not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:  # noqa: BLE001
            # robots.txt 取得失敗はアクセス許可とみなす
            pass
        _robots_cache[base] = rp
    return _robots_cache[base].can_fetch(USER_AGENT, url)


def is_marche_url(url: str) -> bool:
    """URL パスが農業・マルシェ・イベント関連キーワードを含むか判定する。"""
    url_lower = url.lower()
    return any(kw.lower() in url_lower for kw in MARCHE_KEYWORDS)


def is_same_domain(url: str, base_domain: str) -> bool:
    """URL が base_domain と同一ドメインに属するか確認する。"""
    try:
        return urlparse(url).netloc == base_domain
    except Exception:  # noqa: BLE001
        return False


class LinkExtractor(HTMLParser):
    """HTML からリンク URL を抽出するパーサー。"""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self.base_url = base_url
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for name, value in attrs:
                if name == "href" and value:
                    abs_url = urljoin(self.base_url, value)
                    # フラグメント・クエリ除去
                    parsed = urlparse(abs_url)
                    clean = parsed._replace(fragment="", query="").geturl()
                    self.links.append(clean)


def discover_marche_urls(
    city_url: str,
    city_slug: str,
    max_depth: int = 1,
    sleep_sec: float = 1.0,
    dry_run: bool = False,
) -> list[str]:
    """自治体トップページから農業・マルシェページ URL を BFS 探索する。

    Args:
        city_url: 自治体農業ページ URL（既に農業カテゴリに絞り込まれた URL）
        city_slug: 自治体スラグ（ログ用）
        max_depth: 探索深さ上限（ドメイン内のみ）
        sleep_sec: リクエスト間 sleep 秒
        dry_run: True の場合、外部通信なしで空リストを返す

    Returns:
        農業・マルシェ関連ページの URL リスト（重複除去済み）

    Note:
        起点 URL（city_url）自体が農業・マルシェキーワードを含む場合、
        または起点 URL が農業カテゴリページの直接リンクとして指定されている場合、
        起点 URL も収集対象とする（depth=0 で即収集）。
    """
    if dry_run:
        print(f"[gen-csv] dry-run: {city_slug} ({city_url}) — スキップ")
        return []

    base_domain = urlparse(city_url).netloc
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(city_url, 0)]
    found: list[str] = []

    while queue:
        url, depth = queue.pop(0)

        # 正規化（末尾スラッシュ統一）
        if url in visited:
            continue
        visited.add(url)

        # robots.txt チェック
        if not is_allowed_by_robots(url):
            print(f"  [robots] disallowed: {url}")
            continue

        print(f"[discover] depth={depth} {url}")
        html = http_get(url)
        if not html:
            continue

        # 農業・マルシェキーワードチェック
        # depth=0（起点 URL）は CSV で農業カテゴリページとして明示的に指定されているため、
        # キーワードマッチに関わらず常に収集対象とする。
        # （inzai: category/12-1-0-0-0.html、ichikawa: eco03/数値 など、
        #   URL パスにキーワードを含まない農業ページへの対応）
        # depth > 0 の子ページはキーワードマッチで絞り込む。
        if depth == 0 or is_marche_url(url):
            if url not in found:
                found.append(url)
                print(f"  [found] {url}")

        # 深さ上限以内なら子リンクを追加
        if depth < max_depth:
            extractor = LinkExtractor(url)
            try:
                extractor.feed(html)
            except Exception as e:  # noqa: BLE001
                print(f"  [WARN] HTML parse error {url}: {e}", file=sys.stderr)
            for link in extractor.links:
                if link not in visited and is_same_domain(link, base_domain):
                    # バイナリ拡張子の URL はキューに追加しない
                    if is_binary_url(link):
                        print(f"  [SKIP] binary URL: {link}", file=sys.stderr)
                        continue
                    queue.append((link, depth + 1))

        time.sleep(sleep_sec)

    return found


def gen_csv_main(argv: list[str]) -> int:
    """Step① discovery: 自治体リスト → 農業・マルシェページ URL の CSV を生成する。"""
    ap = argparse.ArgumentParser(
        prog="crawl_marche.py gen-csv",
        description="Step① discovery: 自治体トップページから農業・マルシェページ URL を探索して CSV 出力",
    )
    ap.add_argument(
        "--target-csv",
        required=True,
        help="自治体リスト CSV パス（ヘッダ city_url,city_slug,pref または url,slug,pref）",
    )
    ap.add_argument(
        "--out",
        required=True,
        help="出力 CSV パス（ヘッダ page_url,fqdn,city_slug,pref）",
    )
    ap.add_argument("--depth", type=int, default=1, help="BFS 探索深さ上限（デフォルト: 1）")
    ap.add_argument("--sleep", type=float, default=1.0, help="リクエスト間 sleep 秒")
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="外部通信なしで URL リスト生成のみ確認（テスト用）",
    )
    ap.add_argument(
        "--target-domain",
        default="",
        help="処理対象の FQDN（指定した場合、この FQDN の自治体のみ処理する）",
    )
    args = ap.parse_args(argv)

    # 自治体リスト CSV を読み込む
    # CSV フォーマット互換性: city_url/city_slug/pref（旧形式）と url/slug/pref（新形式）の両方に対応
    targets: list[tuple[str, str, str]] = []
    with open(args.target_csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # city_url または url（フォールバック）
            city_url = (row.get("city_url") or row.get("url") or "").strip()
            # city_slug または slug（フォールバック）
            city_slug = (row.get("city_slug") or row.get("slug") or "").strip()
            pref = (row.get("pref") or "").strip()
            if city_url and city_slug:
                # --target-domain 指定時はそのドメインのみフィルタ
                if args.target_domain:
                    row_fqdn = urlparse(city_url).hostname or ""
                    if row_fqdn != args.target_domain:
                        continue
                targets.append((city_url, city_slug, pref))

    if not targets:
        print(f"[gen-csv] 自治体リストが空です: {args.target_csv}", file=sys.stderr)
        return 1

    print(f"[gen-csv] {len(targets)} 自治体を処理します")

    # 出力 CSV 準備
    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    total = 0
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["page_url", "fqdn", "city_slug", "pref"])

        for city_url, city_slug, pref in targets:
            fqdn = urlparse(city_url).netloc
            urls = discover_marche_urls(
                city_url,
                city_slug,
                max_depth=args.depth,
                sleep_sec=args.sleep,
                dry_run=args.dry_run,
            )
            for url in urls:
                row_fqdn = urlparse(url).netloc
                writer.writerow([url, row_fqdn, city_slug, pref])
                total += 1
            print(f"[gen-csv] {city_slug}: {len(urls)} ページ発見")
            _ = fqdn  # 未使用変数の警告抑制

    print(f"[gen-csv] 合計 {total} ページ → {args.out}")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"pages_discovered={total}\n")

    return 0


# ---------------------------------------------------------------------------
# .data/meta.json ヘルパー（crawl_retty.py と同一ロジック）
# ---------------------------------------------------------------------------

def get_meta_path(url: str, out_dir: str = ".") -> str:
    """URL に対応する .data/meta.json のパスを返す。"""
    parsed = urlparse(url)
    fqdn = parsed.netloc
    url_path = parsed.path or "/"
    rel_path = archive_path(fqdn, url_path)
    page_dir = os.path.join(out_dir, os.path.dirname(rel_path))
    return os.path.join(page_dir, ".data", "meta.json")


def is_fresh(meta_path: str, max_age_days: float) -> bool:
    """meta.json が存在し last_crawled が max_age_days 以内なら True。"""
    if max_age_days <= 0:
        return False
    if not os.path.exists(meta_path):
        return False
    try:
        with open(meta_path) as f:
            meta = json.load(f)
        age_days = (time.time() - meta.get("last_crawled", 0)) / 86400
        return age_days < max_age_days
    except Exception:
        return False


def update_meta(url: str, content: bytes, out_dir: str = ".") -> None:
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
# Scrapy Spider（save-html ステップ）
# ---------------------------------------------------------------------------

def save_html_scrapy(
    urls: list[str],
    out_dir: str = ".",
    skip: bool = False,
    max_age_days: float = 7.0,
) -> tuple[int, int, int]:
    """Scrapy で並列クロールして HTML を保存する。

    Returns:
        (saved, skipped, failed) のタプル
    """
    try:
        import scrapy
        from scrapy.crawler import CrawlerProcess
        from scrapy.http import Response
    except ImportError:
        print("[INFO] Scrapy not available, falling back to urllib", file=sys.stderr)
        return save_html_urllib(urls, out_dir, skip, max_age_days)

    class MarcheSpider(scrapy.Spider):
        name = "marche"
        custom_settings = {
            "CONCURRENT_REQUESTS": 4,
            "DOWNLOAD_DELAY": 1.0,
            "AUTOTHROTTLE_ENABLED": True,
            "AUTOTHROTTLE_START_DELAY": 1,
            "AUTOTHROTTLE_MAX_DELAY": 10,
            "AUTOTHROTTLE_TARGET_CONCURRENCY": 2.0,
            "ROBOTSTXT_OBEY": False,
            "DOWNLOADER_MIDDLEWARES": {
                "scrapy.downloadermiddlewares.offsite.OffsiteMiddleware": None,
            },
            "USER_AGENT": USER_AGENT,
            "LOG_LEVEL": "WARNING",
            "DOWNLOAD_TIMEOUT": 30,
        }

        def __init__(
            self,
            target_urls: list[str],
            out_directory: str,
            skip_existing: bool,
            max_age: float = 7.0,
            **kwargs,
        ):
            super().__init__(**kwargs)
            self.target_urls = target_urls
            self.out_directory = out_directory
            self.skip_existing = skip_existing
            self.max_age = max_age
            self.saved = 0
            self.skipped = 0
            self.failed = 0

        def start_requests(self):
            # Scrapy 2.11 以前の互換性のため start_requests も実装する
            for url in self.target_urls:
                meta_path = get_meta_path(url, self.out_directory)
                if self.skip_existing and is_fresh(meta_path, self.max_age):
                    self.skipped += 1
                    continue
                parsed = urlparse(url)
                rel = archive_path(parsed.netloc, parsed.path or "/")
                dest = os.path.join(self.out_directory, rel)
                yield scrapy.Request(
                    url,
                    callback=self.parse,
                    errback=self.errback,
                    meta={"dest": dest},
                )

        async def start(self):
            # Scrapy 2.11+ の StartSpiderMiddleware 対応
            for url in self.target_urls:
                meta_path = get_meta_path(url, self.out_directory)
                if self.skip_existing and is_fresh(meta_path, self.max_age):
                    self.skipped += 1
                    continue
                parsed = urlparse(url)
                rel = archive_path(parsed.netloc, parsed.path or "/")
                dest = os.path.join(self.out_directory, rel)
                yield scrapy.Request(
                    url,
                    callback=self.parse,
                    errback=self.errback,
                    meta={"dest": dest},
                )

        def parse(self, response: "Response"):
            dest: str = response.meta["dest"]
            target_url = response.url
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(response.text)
            update_meta(target_url, response.body, self.out_directory)
            self.saved += 1
            if self.saved % 10 == 0:
                print(f"  [INFO] saved {self.saved} pages", file=sys.stderr)

        def errback(self, failure):
            self.failed += 1
            print(
                f"  [WARN] failed: {failure.request.url}: {failure.value}",
                file=sys.stderr,
            )

        def closed(self, reason):
            print(
                f"[INFO] MarcheSpider closed ({reason}): "
                f"saved={self.saved}, skipped={self.skipped}, failed={self.failed}",
                file=sys.stderr,
            )

    process = CrawlerProcess()
    spider_inst = [None]

    def _on_spider_closed(spider, reason):
        spider_inst[0] = spider

    from scrapy import signals
    crawler = process.create_crawler(MarcheSpider)
    crawler.signals.connect(_on_spider_closed, signal=signals.spider_closed)
    process.crawl(
        crawler,
        target_urls=urls,
        out_directory=out_dir,
        skip_existing=skip,
        max_age=max_age_days,
    )
    process.start()

    sp = spider_inst[0]
    if sp:
        return sp.saved, sp.skipped, sp.failed
    return 0, 0, 0


def save_html_urllib(
    urls: list[str],
    out_dir: str = ".",
    skip: bool = False,
    max_age_days: float = 7.0,
) -> tuple[int, int, int]:
    """urllib で逐次クロール（Scrapy 未インストール時のフォールバック）。"""
    saved, skipped, failed = 0, 0, 0

    for i, url in enumerate(urls, 1):
        # TTL チェック
        if skip:
            meta_path = get_meta_path(url, out_dir)
            if is_fresh(meta_path, max_age_days):
                skipped += 1
                continue

        # robots.txt チェック
        if not is_allowed_by_robots(url):
            print(f"  [robots] disallowed: {url}")
            failed += 1
            continue

        print(f"[fetch {i}/{len(urls)}] {url}")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_type = resp.headers.get_content_type() or ""
                if not content_type.startswith("text/html"):
                    failed += 1
                    continue
                content = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                html = content.decode(charset, errors="replace")

            parsed = urlparse(url)
            fqdn = parsed.netloc
            url_path = parsed.path or "/"
            rel_path = archive_path(fqdn, url_path)
            dest = os.path.join(out_dir, rel_path)
            os.makedirs(os.path.dirname(dest) if os.path.dirname(dest) else ".", exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(html)
            update_meta(url, content, out_dir)
            saved += 1
            print(f"  -> {dest}")
        except urllib.error.HTTPError as e:
            print(f"  [WARN] HTTP {e.code} for {url}", file=sys.stderr)
            failed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  [WARN] fetch failed {url}: {e}", file=sys.stderr)
            failed += 1

        time.sleep(1.0)

    return saved, skipped, failed


def save_html_main(argv: list[str]) -> int:
    """Step② archive: CSV → 生 HTML を archive_path() 規則で保存する（Scrapy 並列）。"""
    ap = argparse.ArgumentParser(
        prog="crawl_marche.py save-html",
        description="Step② archive: CSV の URL リストから生 HTML を archive_path() 規則で保存（Scrapy 並列）",
    )
    ap.add_argument(
        "--csv",
        required=True,
        help="クロール対象 CSV パス（ヘッダ page_url,fqdn,city_slug,pref）",
    )
    ap.add_argument("--sleep", type=float, default=1.0, help="リクエスト間 sleep 秒（urllib フォールバック時）")
    ap.add_argument("--max-count", type=int, default=0, help="最大処理件数（0 = 全件）")
    ap.add_argument(
        "--skip-existing",
        action="store_true",
        help="保存済み HTML をスキップ（TTL チェック: --max-age-days 以内は再クロール不要）",
    )
    ap.add_argument(
        "--max-age-days",
        type=float,
        default=7.0,
        help="この日数より古い .data/meta.json は再クロール対象（0=常に再クロール、デフォルト: 7）",
    )
    ap.add_argument(
        "--out-dir",
        default=".",
        help="HTML 保存先ディレクトリ（デフォルト: カレントディレクトリ）",
    )
    ap.add_argument(
        "--use-urllib",
        action="store_true",
        help="Scrapy を使わず urllib で処理（デバッグ用）",
    )
    args = ap.parse_args(argv)

    # CSV 読み込み
    rows: list[dict] = []
    with open(args.csv, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url = (row.get("page_url") or "").strip()
            if url:
                rows.append({"url": url})

    if args.max_count > 0:
        rows = rows[: args.max_count]

    urls = [r["url"] for r in rows]
    print(f"[save-html] {len(urls)} ページを処理します")

    if not urls:
        print("[save-html] 処理対象 URL が 0 件です")
        # 0件でも gen-csv が 0 ページだった場合は正常終了とする
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as f:
                f.write("pages_saved=0\n")
                f.write("pages_failed=0\n")
        return 0

    if args.use_urllib:
        saved, skipped, failed = save_html_urllib(
            urls, args.out_dir, args.skip_existing, args.max_age_days
        )
    else:
        saved, skipped, failed = save_html_scrapy(
            urls, args.out_dir, args.skip_existing, args.max_age_days
        )

    print(f"\n[save-html] DONE: {saved} saved, {skipped} skipped, {failed} failed")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"pages_saved={saved}\n")
            f.write(f"pages_failed={failed}\n")

    return 0 if saved > 0 or skipped > 0 else 1


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: crawl_marche.py <subcommand> [options]", file=sys.stderr)
        print("  gen-csv   Step① discovery: 自治体リスト → マルシェページ URL の CSV", file=sys.stderr)
        print("  save-html Step② archive: CSV → 生 HTML 保存（Scrapy 並列）", file=sys.stderr)
        return 1

    sub = sys.argv[1]
    if sub == "gen-csv":
        return gen_csv_main(sys.argv[2:])
    elif sub == "save-html":
        return save_html_main(sys.argv[2:])
    else:
        print(f"Unknown subcommand: {sub}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
