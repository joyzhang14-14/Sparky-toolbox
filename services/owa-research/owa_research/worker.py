"""研究容器入口。

用法：
  python -m owa_research.worker research   # stdin 读 ResearchRequest JSON，stdout 最后一行输出 ResearchResult JSON
  python -m owa_research.worker doctor [--smoke]
  python -m owa_research.worker browse     # sparky-toolbox 的 browser 工具用：出站代理 + 浏览器驱动，stdin / stdout 一行一个 JSON
  python -m owa_research.worker parse <文件>…   # 只解析本地文件（sparky-toolbox 的 attachment 工具用，断网容器里跑），stdout 最后一行输出 {"files": [...]}
日志走 stderr，不泄露正文、密钥、cookie 或内部路径。
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

from pydantic import ValidationError

from owa_research.core.config import load_settings
from owa_research.core.doctor import run_doctor
from owa_research.core.errors import ErrorCode
from owa_research.core.research import cancelled_result, failed_result, run_research
from owa_research.core.runtime import Runtime
from owa_research.core.schema import ResearchRequest

log = logging.getLogger("owa.worker")


def _emit(obj: object) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    sys.stdout.flush()


async def _browse() -> int:
    """sparky-toolbox 的 browser 工具用：起出站代理，再起浏览器驱动 agent.mjs（stdin / stdout 直通宿主，一行一个 JSON）。"""
    import os

    from owa_research.isolation.egress_proxy import EgressProxy

    proxy = EgressProxy()
    await proxy.start()
    try:
        agent = str(Path(os.environ["OWA_BROWSER_SERVER"]).with_name("agent.mjs"))
        proc = await asyncio.create_subprocess_exec("node", agent, env={**os.environ, "OWA_PROXY_URL": proxy.url})
        return await proc.wait()
    finally:
        await proxy.stop()


async def _main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 请求行含完整 URL（可能带签名参数），不写日志
    settings = load_settings()
    command = argv[0] if argv else "research"
    artifact_dir = Path(settings.artifact_dir)

    if command == "doctor":
        rt = Runtime.build(settings, None, artifact_dir if artifact_dir.exists() else Path("/tmp/owa-doctor"))
        try:
            _emit(await run_doctor(rt, smoke="--smoke" in argv))
        finally:
            await rt.aclose()
        return 0

    if command == "parse":
        _emit({"files": [await _parse_file(Path(a)) for a in argv[1:]]})
        return 0

    if command == "browse":
        return await _browse()

    if command != "research":
        _emit({"error": {"code": ErrorCode.INVALID_ARGUMENT, "message": f"未知命令 {command}"}})
        return 2

    raw = sys.stdin.read()
    try:
        request = ResearchRequest.model_validate_json(raw)
    except ValidationError as e:
        _emit({"error": {"code": ErrorCode.INVALID_ARGUMENT, "message": "请求不符合 schema: " + "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:5])}})
        return 2

    rt = Runtime.build(settings, request, artifact_dir)
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(run_research(request, rt))

    def _cancel() -> None:  # SIGTERM：置取消标志并中断进行中的 await（模型调用、抓取）
        rt.cancel.set()
        task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _cancel)
    try:
        result = await task
    except asyncio.CancelledError:
        result = cancelled_result(rt)
    except Exception as e:  # noqa: BLE001 — 任何内部错误都要给主会话一个结构化结果，不泄露 stack trace
        log.exception("research crashed")
        result = failed_result(rt, f"{type(e).__name__}")
    finally:
        await rt.aclose()
    _emit(result.model_dump(mode="json"))
    return 0


PARSE_MAX_BYTES = 20 * 1024 * 1024
PARSE_MAX_CHARS = 200_000


async def _parse_file(path: Path) -> dict[str, object]:
    """解析一个本地文件：不联网、不调模型（扫描件没有文字层就如实报告）。只返回文本与少量元信息；
    认不出文字的格式（压缩包、可执行文件等二进制）不算出错，返回 kind unreadable 与大小。"""
    from owa_research.core.errors import ToolError
    from owa_research.readers.document import parse_document, sniff

    try:
        data = path.read_bytes()
        if len(data) > PARSE_MAX_BYTES:
            return {"name": path.name, "error": {"code": ErrorCode.FETCH_FAILED, "message": f"文件超过 {PARSE_MAX_BYTES} 字节"}}
        doc, sniffed = await parse_document(data, url=f"file:{path.name}", model=None)
    except ToolError as e:
        if e.info.code == ErrorCode.UNSUPPORTED_FORMAT:
            return {"name": path.name, "kind": "unreadable", "size": len(data), "mime": sniff(data, url=f"file:{path.name}").mime}
        return {"name": path.name, "error": {"code": e.info.code, "message": e.info.message[:300]}}
    except Exception as e:  # noqa: BLE001 — 坏文件不该让整批失败
        return {"name": path.name, "error": {"code": ErrorCode.UNSUPPORTED_FORMAT, "message": f"解析失败：{type(e).__name__}"}}
    # 表格按行附在文字后面：PDF 文字层里表格的列常被拆开分别排（名字一列、编号一列），只看文字层对不上号
    tables = []
    for t in doc.tables:
        rows = [" | ".join(" ".join(str(c).split()) for c in r) for r in t.rows]
        tables.append(f"## 表格 {t.index + 1}" + (f"（第 {t.page} 页）" if t.page else "") + "\n" + "\n".join(rows))
    table_text = "\n\n".join(tables)[: PARSE_MAX_CHARS // 2]
    text = doc.text[: PARSE_MAX_CHARS - len(table_text)]
    full = text + ("\n\n# 解析出的表格（每行一条，单元格用 | 分隔）\n" + table_text if table_text else "")
    return {"name": path.name, "kind": doc.kind, "mime": sniffed.mime, "title": doc.title, "scanned": doc.scanned,
            "text": full, "tables": len(doc.tables), "truncated": len(doc.text) > len(text), "warnings": doc.warnings[:10]}


def main() -> None:
    try:
        code = asyncio.run(_main(sys.argv[1:]))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
