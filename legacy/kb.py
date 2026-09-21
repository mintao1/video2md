"""Dify 知识库：上传与检索自检。"""
from __future__ import annotations

import os
import sys

from .config import KB_NAME, dataset_api
from .util import redact



# ---------------------------------------------------------------- ⑥ knowledge base（dify_kit DatasetAPI）
def _resolve_dataset() -> str:
    """优先 DIFY_DATASET_ID；否则按名解析，不存在则自动创建「视频知识库」。"""
    ds_id = os.environ.get("DIFY_DATASET_ID", "").strip()
    if ds_id and "00000000" not in ds_id:
        return ds_id
    api = dataset_api()
    try:
        return api.resolve_dataset(name=KB_NAME)  # 有则取 id，无则创建
    except Exception as e:  # noqa: BLE001
        sys.exit(f"❌ 知识库不可用（resolve/create「{KB_NAME}」失败）: {redact(str(e))[:300]}\n"
                 f"   若报 embedding 相关错误：先在 Dify 控制台配置 embedding 模型再重跑")




def kb_upload(title: str, md: str, rlog: RunLog) -> str:
    api = dataset_api()
    ds_id = _resolve_dataset()
    with rlog.span("kb_upload") as sp:
        sp.update(dataset_id=ds_id[:8])
        doc_id, batch = api.upload_text(ds_id, title, md)
        if not doc_id:
            raise RuntimeError("create-by-text 未返回 document id")
        sp.update(doc_id=doc_id[:8])
    status = api.wait_indexed(ds_id, batch or doc_id, timeout=180)
    rlog.info("kb_index", doc_id=doc_id[:8], status=status)
    if "error" in status:
        print(f"  ⚠ 知识库索引状态异常: {status}")
    elif "completed" in status:
        print("  ✅ 知识库索引完成")
    else:
        print(f"  ⚠ 索引仍在进行（{status}），不阻塞后续流程")
    return doc_id



def kb_selfcheck(doc_id: str, meta: dict, rlog: RunLog) -> bool:
    api = dataset_api()
    ds_id = _resolve_dataset()
    queries = [q for q in [meta.get("title", ""), meta.get("summary", "")[:50],
                           " ".join(meta.get("tags", [])[:4])] if len(q) >= 4][:3]
    if not queries:
        return False
    ok_all = True
    with rlog.span("kb_selfcheck"):
        for q in queries:
            r = api.retrieve(ds_id, q, top_k=3)
            recs = r.get("records", [])
            hit = any((rec.get("segment") or {}).get("document_id") == doc_id for rec in recs)
            print(f"  {'✅' if hit else '⚠️'} 检索自检「{q[:24]}」{'命中' if hit else '未命中'}")
            rlog.info("kb_retrieve", query=q[:24], hit=hit)
            ok_all = ok_all and hit
    print("SELF-CHECK " + ("PASS" if ok_all else "WARN"))
    return ok_all
