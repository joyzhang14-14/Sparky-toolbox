"""bge-m3 语义向量（search_skills、search-history 的「语义相近」）：BAAI 官方 ONNX 版，用 onnxruntime + tokenizers 在 CPU 跑。
每次搜索临时加载（约 3 秒、1.5 GB 内存），用完就放，不常驻（2026-10-15 Joy 定）。
模型文件在 MODEL_DIR：HuggingFace 缓存里 BAAI/bge-m3 的 onnx/ 的硬链接（onnxruntime 要求外部权重 model.onnx_data 和 model.onnx 在同一个目录，缓存里两者在不同的 blobs 子目录）。
similar 的向量缓存在调用方给的 sqlite 库的 vectors 表（键 + 文本的 sha1 → 向量，文本变了才重算）；消息的向量存在消息库自己的 vec 列（search-history）。"""
from __future__ import annotations

import hashlib
import sqlite3

import numpy as np

from control_center.paths import PLATFORM_HOME

MODEL_DIR = PLATFORM_HOME / "models" / "bge-m3-onnx"   # 平台用户（Joy）家目录下的 models/（sparkle 组可读；工具以 sparky 跑，家目录不是 Joy 的，不用 Path.home()）
MAX_TOKENS = 512                                      # 每段文本最多取前 512 个 token
BATCH = 16
DIM = 1024
PAD = 1                                               # bge-m3（XLM-R）的 <pad>


def vectors(texts: list[str]) -> np.ndarray:
    """texts → 每行一个归一化的向量（float32）。加载一次模型算完全部。"""
    import onnxruntime as ort
    from tokenizers import Tokenizer

    session = ort.InferenceSession(str(MODEL_DIR / "model.onnx"), providers=["CPUExecutionProvider"])
    tok = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
    tok.enable_truncation(MAX_TOKENS)
    encs = tok.encode_batch(texts)
    order = sorted(range(len(texts)), key=lambda i: len(encs[i].ids))   # 长短相近的一批，少补齐（实测快一倍多）
    v = np.zeros((len(texts), DIM), dtype=np.float32)
    for i in range(0, len(order), BATCH):
        part = order[i:i + BATCH]
        n = max(len(encs[j].ids) for j in part)
        ids = np.array([encs[j].ids + [PAD] * (n - len(encs[j].ids)) for j in part], dtype=np.int64)
        mask = np.array([[1] * len(encs[j].ids) + [0] * (n - len(encs[j].ids)) for j in part], dtype=np.int64)
        v[part] = np.asarray(session.run(["sentence_embedding"], {"input_ids": ids, "attention_mask": mask})[0])
    return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)


def similar(query: str, texts: dict[str, str], db: sqlite3.Connection, floor: float, margin: float) -> list[str]:
    """texts {键: 文本} 里和 query 语义相近的键（余弦不低于 floor、离最近的差不超过 margin），越近越前。没有算过（或文本变了）的这次和 query 一起算、存进 db 的 vectors 表。"""
    if not texts:
        return []
    db.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, sha TEXT NOT NULL, vec BLOB NOT NULL)")
    sha = {k: hashlib.sha1(t.encode()).hexdigest() for k, t in texts.items()}
    have = {k: np.frombuffer(v, dtype=np.float32) for k, s, v in db.execute("SELECT key, sha, vec FROM vectors") if sha.get(k) == s}
    todo = [k for k in texts if k not in have]
    new = vectors([query, *(texts[k] for k in todo)])
    with db:
        db.executemany("INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)", [(k, sha[k], new[i + 1].tobytes()) for i, k in enumerate(todo)])
    have.update((k, new[i + 1]) for i, k in enumerate(todo))
    keys = list(texts)
    return [keys[i] for i in near(np.stack([have[k] for k in keys]) @ new[0], floor, margin)]


def near(sims: np.ndarray, floor: float, margin: float) -> list[int]:
    """余弦相似度 sims 里算「相近」的下标（不低于 floor、离最近的差不超过 margin），越近越前。"""
    cut = max(floor, float(sims.max()) - margin)
    return [int(i) for i in np.argsort(-sims, kind="stable") if sims[i] >= cut]
