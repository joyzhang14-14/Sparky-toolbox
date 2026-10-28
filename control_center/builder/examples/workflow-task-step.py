# workflow 任务要资源的一步（tasks/nightly-sft/train.py）：收到 SIGTERM 就存好进度、以 75 退出；重跑时从存档接着
import json
import os
import signal
import sys
from pathlib import Path

WORK = Path("/home/sparky/workspace/nightly-sft")
CKPT = WORK / "ckpt.json"
stop = False


def on_term(*_):
    global stop
    stop = True


signal.signal(signal.SIGTERM, on_term)
done = json.loads(CKPT.read_text())["done"] if CKPT.exists() else 0   # 重跑（STB_RESUME=1）或上次被打断：从存档接着
batches = sorted((WORK / "batches").glob("*.jsonl"))
if not batches:
    sys.exit(3)                                                        # 没事可做：运行到此结束，不发消息
for i in range(done, len(batches)):
    if stop:
        sys.exit(75)                                                   # 进度已存好：让位
    ...                                                                # 训练这一批
    tmp = CKPT.with_suffix(".tmp")
    tmp.write_text(json.dumps({"done": i + 1}))
    os.replace(tmp, CKPT)                                              # 原子写：断电也不会留半截存档
Path(os.environ["STB_REPLY_FILE"]).write_text(f"nightly-sft：训完 {len(batches)} 批")
