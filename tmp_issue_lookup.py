"""临时脚本：按任务编号定位猪齿鱼 issue 加密 id。"""
import json
import sys

from zhenyun_pangu_mcp import choerodon

kw = sys.argv[1] if len(sys.argv) > 1 else "prod-bug-214908"

for k in (kw, kw.split("-")[-1]):
    try:
        items = choerodon.search_issues(keyword=k, size=20)
    except Exception as e:
        print(f"[keyword={k}] ERROR {type(e).__name__}: {e}")
        continue
    print(f"[keyword={k}] total={len(items)}")
    for it in items:
        print(json.dumps(it, ensure_ascii=False))
