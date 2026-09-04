"""兼容旧命令：默认使用 NVIDIA 回填 knowledge_docs.embedding。

新的多 provider / 批量 / 断点续跑能力请使用 rebuild_embeddings.py。
此脚本保留旧参数，确保原有运维入口不会失效。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rebuild_embeddings import main  # noqa: E402


if __name__ == "__main__":
    # 旧 --all 语义等价于新脚本的 --force；旧脚本只处理 knowledge_docs。
    argv = ["--provider", "nvidia", "--table", "knowledge_docs"]
    args = sys.argv[1:]
    if "--all" in args:
        args = ["--force", *[arg for arg in args if arg != "--all"]]
    argv.extend(args)
    raise SystemExit(main(argv))
