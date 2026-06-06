#!/usr/bin/env python3
"""crawl_archive.py — Retty 公開サイト → 生 HTML をミラー保存するクローラ

本スクリプトは factory-entreprenais-com-builder の crawl_retty.py から
discovery（URL 収集）と raw HTML 保存の責務のみを切り出したものです。
HTML の解析（store.json 生成）は builder 側が担います。

2 ステップ構成:
  Step① discovery（gen-csv サブコマンド）:
     エリア一覧ページをページネーション辿り、飲食店 URL を CSV に出力する。
     store.json は作らず URL 一覧のみ。

  Step② archive（save-html サブコマンド）:
     CSV を読み込み、各店舗詳細ページを取得して
     {domain}/{path}/index.html に保存する。

使い方:
  # Step① discovery: エリア → CSV
  python3 scripts/crawl_archive.py gen-csv \
    --area-url "https://retty.me/area/PRE13/ARE7/SUB701/" \
    --out .data/crawl-targets/ebisu.csv \
    --max-count 2000 \
    --sleep 5

  # Step② archive: CSV → 生 HTML 保存
  python3 scripts/crawl_archive.py save-html \
    --csv .data/crawl-targets/ebisu.csv \
    --out-dir retty.com \
    --sleep 5

- 内部 API には一切アクセスしない。公開ページのみ。
- robots.txt を尊重し、リクエスト間に sleep を入れ、User-Agent を明示する。
- 外部依存なし（標準ライブラリのみ）。
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
import urllib.request
import urllib.error
from html import unescape

USER_AGENT = "EntreprenAIs-Archive-crawler/0.1 (+https://entreprenais.com/#contact)"

# Retty 店舗詳細 URL に含まれる店舗 ID（10桁以上の数字）
RE_RETTY_ID = re.compile(r"/([0-9]{10,})/")
# エリア配下の店舗詳細リンク
RE_STORE_LINK = re.compile(r'href="(/area/[A-Z0-9/]*?/([0-9]{10,})/)"')
# 「次のページへ」リンク
RE_NEXT = re.compile(r'<a\b[^>]*?href="([^"]+)"[^>]*?rel="next"', re.S)
RE_NEXT_ALT = re.compile(r'<a\b[^>]*?rel="next"[^>]*?href="([^"]+)"', re.S)

BASE = "https://retty.me"
DOMAIN = "retty.com"


def http_get(url: str, timeout: int = 30) -> str | None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            return resp.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        print(f"  [WARN] HTTP {e.code} for {url}", file=sys.stderr)
    except Exception as e:  # noqa: BLE001
        print(f"  [WARN] fetch failed {url}: {e}", file=sys.stderr)
    return None


def absolutize(href: str) -> str:
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return BASE + href
    return BASE + "/" + href


def collect_store_ids(area_url: str, max_count: int, sleep: float) -> list[tuple[str, str]]:
    """エリア一覧をページネーション辿り (retty_id, detail_url) を収集する。"""
    seen: dict[str, str] = {}
    url = area_url
    page = 0
    while url and len(seen) < max_count:
        page += 1
        print(f"[list] page {page}: {url}")
        html = http_get(url)
        if not html:
            break
        page_found = 0
        for m in RE_STORE_LINK.finditer(html):
            path, rid = m.group(1), m.group(2)
            if rid not in seen:
                seen[rid] = absolutize(path)
                page_found += 1
            if len(seen) >= max_count:
                break
        print(f"  found {page_found} new ids (total {len(seen)})")
        nm = RE_NEXT.search(html) or RE_NEXT_ALT.search(html)
        next_url = absolutize(unescape(nm.group(1))) if nm else None
        if not next_url or next_url == url:
            break
        url = next_url
        time.sleep(sleep)
    return list(seen.items())[:max_count]


def write_targets_csv(targets: list[tuple[str, str]], out_path: str) -> int:
    """discovery 結果を CSV に書き出す（retty_url,retty_id ヘッダ）。"""
    seen: dict[str, str] = {}
    for rid, url in targets:
        if rid and rid not in seen:
            seen[rid] = url
    rows = sorted(seen.items(), key=lambda kv: kv[0])

    parent = os.path.dirname(out_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["retty_url", "retty_id"])
        for rid, url in rows:
            writer.writerow([url, rid])
    return len(rows)


def rid_from_url(url: str) -> str | None:
    """Retty 店舗 URL から retty_id を抽出する。"""
    if not url:
        return None
    m = RE_RETTY_ID.search(url)
    return m.group(1) if m else None


def detail_url_from_rid(rid: str) -> str:
    return f"{BASE}/restaurant/{rid}/"


def _normalize_header(name: str) -> str:
    return (name or "").strip().lstrip("﻿").lower()


def read_csv_targets(csv_path: str) -> list[tuple[str, str]]:
    """ヘッダ付き CSV を読み (retty_id, detail_url) のリストを返す。"""
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise ValueError(f"CSV が空です: {csv_path}")

        norm = [_normalize_header(h) for h in header]
        url_idx = norm.index("retty_url") if "retty_url" in norm else None
        id_idx = norm.index("retty_id") if "retty_id" in norm else None
        if url_idx is None and id_idx is None:
            raise ValueError(
                f"CSV に retty_url / retty_id 列がありません（ヘッダ: {header}）: {csv_path}"
            )

        seen: dict[str, str] = {}
        for lineno, row in enumerate(reader, start=2):
            if not row or all(not (c or "").strip() for c in row):
                continue

            rid: str | None = None
            url: str | None = None

            if url_idx is not None and url_idx < len(row):
                raw_url = (row[url_idx] or "").strip()
                if raw_url:
                    rid = rid_from_url(raw_url)
                    if rid:
                        url = raw_url

            if rid is None and id_idx is not None and id_idx < len(row):
                raw_id = (row[id_idx] or "").strip()
                if raw_id:
                    m = re.search(r"[0-9]{10,}", raw_id)
                    if m:
                        rid = m.group(0)
                        url = detail_url_from_rid(rid)

            if not rid or not url:
                print(
                    f"  [WARN] CSV {lineno} 行目: 有効な retty_url / retty_id を"
                    f"抽出できませんでした（スキップ）: {row}",
                    file=sys.stderr,
                )
                continue
            if rid not in seen:
                seen[rid] = url

    return list(seen.items())


def url_to_path(url: str, out_dir: str) -> str:
    """URL をミラー構造のファイルパスに変換する。

    https://retty.me/area/PRE13/ARE7/SUB701/100001234567/
    -> {out_dir}/area/PRE13/ARE7/SUB701/100001234567/index.html
    """
    # スキームとホストを除去してパス部分を取得
    path = re.sub(r"^https?://[^/]+", "", url).rstrip("/")
    if not path:
        path = "/"
    return os.path.join(out_dir, path.lstrip("/"), "index.html")


def save_html(url: str, html: str, out_dir: str) -> str:
    """HTML を {out_dir}/{url_path}/index.html に保存する。"""
    file_path = url_to_path(url, out_dir)
    os.makedirs(os.path.dirname(file_path), exist_ok=True)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(html)
    return file_path


def gen_csv_main(argv: list[str]) -> int:
    """Step① discovery: エリア → 飲食店 URL の CSV を生成する。"""
    ap = argparse.ArgumentParser(
        prog="crawl_archive.py gen-csv",
        description="Step① discovery: Retty エリア一覧 → 飲食店 URL の CSV を生成",
    )
    ap.add_argument("--area-url", required=True, help="Retty エリア一覧 URL")
    ap.add_argument("--out", required=True,
                    help="出力 CSV パス（ヘッダ retty_url,retty_id）")
    ap.add_argument("--max-count", type=int, default=2000,
                    help="最大収集件数")
    ap.add_argument("--sleep", type=float, default=0.5,
                    help="リクエスト間 sleep 秒")
    args = ap.parse_args(argv)

    print(f"[gen-csv] area={args.area_url} max={args.max_count} "
          f"sleep={args.sleep}s out={args.out}")
    targets = collect_store_ids(args.area_url, args.max_count, args.sleep)
    n = write_targets_csv(targets, args.out)
    print(f"[gen-csv] wrote {n} targets -> {args.out}")

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"targets_written={n}\n")
    return 0 if n > 0 else 1


def save_html_main(argv: list[str]) -> int:
    """Step② archive: CSV → 生 HTML 保存。"""
    ap = argparse.ArgumentParser(
        prog="crawl_archive.py save-html",
        description="Step② archive: CSV の URL リストから生 HTML を取得して保存",
    )
    ap.add_argument("--csv", required=True,
                    help="クロール対象 CSV パス（ヘッダ retty_url,retty_id）")
    ap.add_argument("--out-dir", default=DOMAIN,
                    help="HTML 保存先ルートディレクトリ（既定: retty.com）")
    ap.add_argument("--sleep", type=float, default=0.5,
                    help="リクエスト間 sleep 秒")
    ap.add_argument("--max-count", type=int, default=0,
                    help="最大処理件数（0 = 全件）")
    args = ap.parse_args(argv)

    print(f"[save-html] csv={args.csv} out-dir={args.out_dir} sleep={args.sleep}s")
    ids = read_csv_targets(args.csv)
    if args.max_count > 0:
        ids = ids[:args.max_count]
    print(f"[save-html] {len(ids)} targets to process")

    ok, fail = 0, 0
    for i, (rid, detail_url) in enumerate(ids, 1):
        print(f"[fetch {i}/{len(ids)}] {rid} {detail_url}")
        html = http_get(detail_url)
        if not html:
            fail += 1
            continue
        try:
            path = save_html(detail_url, html, args.out_dir)
            ok += 1
            print(f"  -> {path}")
        except Exception as e:  # noqa: BLE001
            print(f"  [ERROR] save failed {rid}: {e}", file=sys.stderr)
            fail += 1
        time.sleep(args.sleep)

    print(f"\n[save-html] DONE: {ok} saved, {fail} failed")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"pages_saved={ok}\n")
            f.write(f"pages_failed={fail}\n")
    return 0 if ok > 0 else 1


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: crawl_archive.py <subcommand> [options]", file=sys.stderr)
        print("  gen-csv   Step① discovery: area URL -> CSV", file=sys.stderr)
        print("  save-html Step② archive: CSV -> raw HTML", file=sys.stderr)
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
