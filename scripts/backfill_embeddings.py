"""一次性脚本：为存量 knowledge_docs 知识批量生成 NVIDIA embedding 并回填。

依赖 zhenyun-pangu-mcp 的 supabase_client / embedding 服务（原 knowledge-ops-mcp 整合后保留于此）。

使用前提：
  1. 已按 schema.sql 新建 embedding 列与 match_knowledge_docs RPC 函数。
  2. .env 中已配置 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY / NVIDIA_API_KEY。

运行（需在 zhenyun-pangu-mcp 项目中，uv 管理）：
  uv run python scripts/backfill_embeddings.py            # 仅回填 embedding 为空的知识
  uv run python scripts/backfill_embeddings.py --all      # 全部重算（覆盖已有 embedding）
  uv run python scripts/backfill_embeddings.py --limit 50 # 仅处理前 N 条（调试用）
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

# 复用 zhenyun-pangu-mcp 的统一 Supabase 客户端与 Embedding 服务
from zhenyun_pangu_mcp import supabase_client as sb  # noqa: E402


def _compose_embed_text(r: dict) -> str:
    """拼装用于生成向量的文本（与 supabase_client.EmbeddingService._compose_knowledge 一致）。"""
    parts = [
        r.get("title") or "", r.get("knowledge_type") or "", r.get("system") or "",
        r.get("module") or "", r.get("summary") or "",
        " ".join(r.get("tags") or []), " ".join(r.get("core_tables") or []),
        r.get("content_md") or "",
    ]
    return "\n".join(x for x in parts if x).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 knowledge_docs 的 embedding 向量")
    parser.add_argument("--all", action="store_true", help="重算全部知识（覆盖已有 embedding）")
    parser.add_argument("--limit", type=int, default=0, help="仅处理前 N 条（调试用，0=不限制）")
    args = parser.parse_args()

    if not sb.embedding.available:
        print("❌ 未配置 NVIDIA_API_KEY，无法生成 embedding。请先在 .env 配置后重试。")
        return 1

    table = sb.config.KNOWLEDGE_TABLE
    params: dict = {"select": "*"}
    if not args.all:
        params["embedding"] = "is.null"
    if args.limit:
        params["limit"] = args.limit
    rows = sb._rest(table, params=params)

    total = len(rows)
    print(f"[{datetime.now():%H:%M:%S}] 待处理知识数：{total}")
    if total == 0:
        print("无需回填。")
        return 0

    ok = 0
    failed = 0
    for i, r in enumerate(rows, 1):
        rid = r.get("id")
        text = _compose_embed_text(r)
        emb = sb.embedding.embed(text, input_type="passage") if text else None
        if emb is None:
            print(f"  [{i}/{total}] id={rid} embedding 生成失败，跳过")
            failed += 1
            time.sleep(0.3)
            continue
        url = f"{sb.base_url()}/rest/v1/{table}?id=eq.{rid}"
        sb._rest_request("PATCH", url, body={"embedding": sb.embedding.to_literal(emb)})
        ok += 1
        print(f"  [{i}/{total}] id={rid} 已回填 embedding（维度 {len(emb)}）")
        time.sleep(0.2)

    print(f"[{datetime.now():%H:%M:%S}] 完成：成功 {ok}，失败 {failed}。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
