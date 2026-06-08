#!/usr/bin/env python3
"""shard_targets.py — CSV ターゲット一覧を GHA matrix 用バッチに分割する

retty / marche 両クローラで共用できる汎用シャーディングスクリプト。
入力 CSV の行数を --batch-size で分割し、GHA matrix JSON を GITHUB_OUTPUT に出力する。

使い方:
  # GITHUB_OUTPUT 書き出し（GHA matrix ジョブ用）
  python3 scripts/shard_targets.py \\
    --csv .data/crawl-targets/ebisu.csv \\
    --batch-size 100 \\
    --output-json

  # --group-by-domain: 同一 FQDN を同一シャードに集約（retty rate limit 対策）
  python3 scripts/shard_targets.py \\
    --csv .data/crawl-targets/ebisu.csv \\
    --group-by-domain \\
    --output-json

  # 標準出力のみ確認
  python3 scripts/shard_targets.py \\
    --csv .data/crawl-targets/ebisu.csv \\
    --batch-size 50

出力 JSON フォーマット（通常モード）:
  {"include": [{"shard_id": 0, "start": 0, "end": 100}, ...]}

  - shard_id: 0 始まりの連番（matrix ジョブの識別子）
  - start: CSV の対象開始行インデックス（ヘッダ除く）
  - end: CSV の対象終了行インデックス（exclusive）

出力 JSON フォーマット（--group-by-domain モード）:
  {"include": [{"shard_id": 0, "domain": "retty.me", "count": 1397}, ...]}

  - shard_id: 0 始まりの連番
  - domain: FQDN
  - count: そのドメインの行数

GHA matrix 上限（256）対応:
  シャード数が 256 を超える場合は --batch-size を自動的に大きくし
  256 シャード以内に収める。警告を stderr に出力する。

使用例（GHA matrix ジョブ）:
  jobs:
    shard:
      outputs:
        matrix: ${{ steps.gen.outputs.matrix }}
      steps:
        - id: gen
          run: python3 scripts/shard_targets.py --csv ... --batch-size 100 --output-json
    crawl:
      strategy:
        matrix: ${{ fromJson(needs.shard.outputs.matrix) }}
      steps:
        - run: python3 ... --shard-id ${{ matrix.shard_id }} --batch-size <N>
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from urllib.parse import urlparse

GHA_MATRIX_MAX = 256


def read_rows_from_csv(csv_path: str) -> list[dict]:
    """CSV の全行を dict のリストとして返す。

    --group-by-domain モード用。全カラムを保持する。
    """
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(dict(row))
    return rows


def _extract_url_from_row(row: dict) -> str:
    """行から URL を抽出する。

    URL カラムの優先順:
      1. "url" カラム（存在し、空でない場合）
      2. "city_url" カラム（marche ターゲット CSV 用）
      3. "retty_url" カラム
    """
    for key in ("url", "city_url", "retty_url"):
        val = (row.get(key) or "").strip()
        if val:
            return val
    return ""


def group_by_domain(rows: list[dict]) -> list[list[dict]]:
    """URL の FQDN でレコードをグルーピングし、グループのリストを返す。

    URL は以下の優先順で参照する:
      1. "url" カラム（存在し、空でない場合）
      2. "city_url" カラム（marche ターゲット CSV 用）
      3. "retty_url" カラム
    FQDN が取得できない行は "unknown" グループに入る。
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        url = _extract_url_from_row(row)
        try:
            fqdn = urlparse(url).hostname or "unknown"
        except Exception:
            fqdn = "unknown"
        groups.setdefault(fqdn, []).append(row)
    return list(groups.values())


def build_matrix_by_domain(rows: list[dict]) -> dict:
    """ドメイングループを GHA matrix dict に変換する。

    URL カラムの優先順は _extract_url_from_row() に従う（url / city_url / retty_url）。

    Returns:
        {"include": [{"shard_id": 0, "domain": "retty.me", "count": 42}, ...]}
    """
    groups: dict[str, list[dict]] = {}
    for row in rows:
        url = _extract_url_from_row(row)
        try:
            fqdn = urlparse(url).hostname or "unknown"
        except Exception:
            fqdn = "unknown"
        groups.setdefault(fqdn, []).append(row)

    include = [
        {"shard_id": i, "domain": domain, "count": len(group_rows)}
        for i, (domain, group_rows) in enumerate(groups.items())
    ]
    return {"include": include}


def count_csv_rows(csv_path: str) -> int:
    """ヘッダ行を除いた CSV の有効行数を返す。"""
    count = 0
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        try:
            next(reader)  # ヘッダをスキップ
        except StopIteration:
            return 0
        for row in reader:
            if row and any((c or "").strip() for c in row):
                count += 1
    return count


def build_shards(total: int, batch_size: int) -> list[dict]:
    """total 行を batch_size で分割したシャードリストを返す。

    GHA matrix 上限（256）を超える場合は batch_size を自動調整する。
    """
    if total == 0:
        return []

    # GHA matrix 上限チェック
    num_shards = math.ceil(total / batch_size)
    if num_shards > GHA_MATRIX_MAX:
        adjusted_batch = math.ceil(total / GHA_MATRIX_MAX)
        print(
            f"[shard_targets] WARN: {num_shards} shards exceeds GHA matrix max ({GHA_MATRIX_MAX}). "
            f"Adjusting batch_size: {batch_size} -> {adjusted_batch}",
            file=sys.stderr,
        )
        batch_size = adjusted_batch
        num_shards = math.ceil(total / batch_size)

    shards = []
    for i in range(num_shards):
        start = i * batch_size
        end = min(start + batch_size, total)
        shards.append({"shard_id": i, "start": start, "end": end})
    return shards


def main() -> int:
    ap = argparse.ArgumentParser(
        description="CSV ターゲット一覧を GHA matrix 用バッチに分割する"
    )
    ap.add_argument("--csv", required=True, help="入力 CSV パス（ヘッダ行 retty_url,retty_id 等）")
    ap.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="1 シャードあたりの最大行数（デフォルト: 100）",
    )
    ap.add_argument(
        "--group-by-domain",
        action="store_true",
        help="同一 FQDN の URL を同一シャードに集約する（retty rate limit 対策）",
    )
    ap.add_argument(
        "--output-json",
        action="store_true",
        help="GITHUB_OUTPUT に matrix JSON を書き出す（GHA 用）",
    )
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        print(f"[shard_targets] ERROR: CSV not found: {args.csv}", file=sys.stderr)
        return 1

    if args.group_by_domain:
        rows = read_rows_from_csv(args.csv)
        print(
            f"[shard_targets] csv={args.csv} total_rows={len(rows)} mode=group-by-domain",
            file=sys.stderr,
        )
        matrix = build_matrix_by_domain(rows)
        n_shards = len(matrix["include"])
        print(f"[shard_targets] generated {n_shards} domain shards", file=sys.stderr)
        for entry in matrix["include"]:
            print(
                f"  shard {entry['shard_id']:3d}: domain={entry['domain']} count={entry['count']}",
                file=sys.stderr,
            )
    else:
        total = count_csv_rows(args.csv)
        print(
            f"[shard_targets] csv={args.csv} total_rows={total} batch_size={args.batch_size}",
            file=sys.stderr,
        )
        shards = build_shards(total, args.batch_size)
        matrix = {"include": shards}
        n_shards = len(shards)
        print(f"[shard_targets] generated {n_shards} shards", file=sys.stderr)

    matrix_json = json.dumps(matrix, separators=(",", ":"))

    if args.output_json:
        print(matrix_json)
        gh_out = os.environ.get("GITHUB_OUTPUT")
        if gh_out:
            with open(gh_out, "a", encoding="utf-8") as f:
                f.write(f"matrix={matrix_json}\n")
            print(f"[shard_targets] written to GITHUB_OUTPUT", file=sys.stderr)
    else:
        print(matrix_json)

    return 0


if __name__ == "__main__":
    sys.exit(main())
