"""以图搜图服务插件的加载器（core/image_engines.py）：扫描、跳过 _ 开头的、约定缺项报错、默认服务（第一个可用的）、网址限制、结果格式、builder 的静态检查。"""
import pytest

from owa_research.core import image_engines as ie
from owa_research.core.errors import ToolError

GOOD = '''from owa_research.core.errors import ErrorCode, ToolError

NAME = "{name}"
DESCRIPTION = "测试"
KEY = {key}
URLS = ["https://api.{name}.example/v1"]
{extra}

async def search(image, key, http):
    return {{"sites": [], "tags": []}}
'''


def write(d, name, key="None", extra="", text=None):
    (d / f"{name}.py").write_text(text if text is not None else GOOD.format(name=name, key=key, extra=extra))


def test_scan_skips_underscore_and_picks_default(tmp_path):
    write(tmp_path, "zeta")
    write(tmp_path, "alpha", key='"STB_ALPHA_API_KEY"')
    (tmp_path / "_wip.py").write_text("坏的也不管")
    (tmp_path / "notes.txt").write_text("x")
    got = ie.scan(tmp_path)
    assert list(got) == ["alpha", "zeta"] and got["alpha"].key == "STB_ALPHA_API_KEY" and got["zeta"].urls == ("https://api.zeta.example/v1",)
    assert ie.default_name(got, {"alpha": "k"}) == "alpha"                  # 第一个可用的（按文件名排序）
    assert ie.default_name(got, {}) == "zeta"                                # alpha 没 key：跳过
    assert ie.scan(tmp_path / "missing") == {} and ie.default_name({}, {}) is None


@pytest.mark.parametrize("text, why", [
    (GOOD.format(name="x", key="None", extra="").replace('DESCRIPTION = "测试"\n', ""), "缺 DESCRIPTION"),
    (GOOD.format(name="x", key="None", extra="").replace('URLS = ["https://api.x.example/v1"]\n', ""), "缺 URLS"),
    (GOOD.format(name="x", key="None", extra="").replace('NAME = "x"', 'NAME = "y"'), "要和文件名一致"),
    (GOOD.format(name="x", key='"STB_OTHER_API_KEY"', extra=""), "KEY"),                       # 不能借别的凭证
    (GOOD.format(name="x", key="None", extra="").replace("https://api", "http://api"), "https"),
    (GOOD.format(name="x", key="None", extra="").replace("async def search(image, key, http)", "def search(image, key, http)"), "async def"),
    (GOOD.format(name="x", key="None", extra="").replace("(image, key, http)", "(image, http)"), r"\(image, key, http\)"),
    (GOOD.format(name="x", key="None", extra="").replace("async def search", "async def find"), "缺 async def search"),
    (GOOD.format(name="x", key="None", extra="").replace('URLS = [', 'URLS = BASE + ['), "字面量"),
    ("NAME = (", "语法错"),
])
def test_contract_errors(tmp_path, text, why):
    write(tmp_path, "x", text=text)
    with pytest.raises(ie.EngineError, match=why):
        ie.scan(tmp_path)


def test_url_allowed():
    urls = ("https://yandex.com/images/search",)
    assert ie.url_allowed("https://yandex.com/images/search?rpt=imageview&url=https://a/b", urls)
    for bad in ("http://yandex.com/images/search", "https://yandex.com.evil.example/images/search", "https://yandex.com/other", "https://evil.example/?u=https://yandex.com/images/search"):
        assert not ie.url_allowed(bad, urls)


def test_normalize():
    out = ie.normalize("x", {"sites": [{"url": "https://a.example/1", "title": "t" * 900, "similarity": 91.5, "urls": ["https://b.example"]},
                                       {"url": "ftp://x", "title": "drop"}, {"url": "", "title": "no link"}],
                             "tags": ["a"], "note": "n", "total_sites": 3})
    assert [s["url"] for s in out["sites"]] == ["https://a.example/1", ""] and len(out["sites"][0]["title"]) == 500
    assert out["tags"] == ["a"] and out["note"] == "n" and out["total_sites"] == 3
    assert ie.site_urls(out) == ["https://a.example/1", "https://b.example"]
    for bad in (None, {"sites": []}, {"sites": [{"title": "t"}], "tags": []}, {"sites": [], "tags": [1]}, {"sites": [], "tags": [], "raw": 1},
                {"sites": [{"url": "https://a", "title": "t", "x": {"y": 1}}], "tags": []}):
        with pytest.raises(ToolError, match="格式不对"):
            ie.normalize("x", bad)


def test_check_source_rules():
    base = GOOD.format(name="x", key="None", extra="")
    assert ie.check_source("x", base) == []
    assert ie.check_source("x", base.replace('URLS = [', 'import re\nURLS = [')) == []
    for snippet, why in (("import os", "import os"), ("import httpx", "import httpx"), ("from pathlib import Path", "from pathlib"),
                         ("from . import y", "from ."), ("V = open('/etc/passwd').read()", "open"), ("V = getattr(object, 'x')", "getattr"),
                         ("V = http._fetcher", "._fetcher"), ("import os.path", "os.path"), ("V = eval('1')", "eval")):
        probs = ie.check_source("x", base.replace("async def search", snippet + "\n\n\nasync def search"))
        assert probs and why in probs[0], (snippet, probs)
    assert "缺 KEY" in ie.check_source("x", base.replace("KEY = None\n", ""))[0]

