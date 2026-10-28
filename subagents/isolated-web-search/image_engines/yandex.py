"""Yandex 以图搜图（非官方接口，09-30 实测）：POST 上传拿 cbirId → GET 结果页 → 页面内嵌 data-state 里的 initialState.cbirSites（含这张图的网站）与 cbirTags。"""
import html as html_lib
import json
import re

from owa_research.core.errors import ErrorCode, ToolError

NAME = "yandex"
DESCRIPTION = ("什么图都能搜（照片、截图、冷门或新的同人图），给出含这张图的网站（标题、链接，常带角色名、画师、作品 tag）和识别标签；"
               "是非官方接口，偶尔会失败，结果里可能有成人网站。照片、截图直接用它；插画在别的服务没结果时再用它")
KEY = None
URLS = ["https://yandex.com/images/search"]
MAX_SITES = 15


async def search(image, key, http):
    r = await http.post_form(URLS[0], params={"rpt": "imageview", "format": "json",
                                              "request": json.dumps({"blocks": [{"block": "b-page_type_search-by-image__link"}]})},
                             files={"upfile": ("blob", image.data, image.mime)})
    try:
        pp = r.json()["blocks"][0]["params"]
        page_url = http.url(URLS[0], {"rpt": "imageview", "url": pp["originalImageUrl"], "cbir_id": pp["cbirId"], "cbir_page": "sites", "lang": "en"})
    except (ValueError, KeyError, IndexError, TypeError):
        raise ToolError(ErrorCode.SEARCH_UNAVAILABLE, f"Yandex 上传返回 {r.status_code}，格式不认识（接口可能改了）；换别的服务或按内容搜", stage="search") from None
    page = await http.get(page_url)
    state = page_state(page.content.decode("utf-8", errors="replace"))
    if state is None:
        raise ToolError(ErrorCode.SEARCH_UNAVAILABLE, "Yandex 结果页里没找到数据（可能出了验证码或页面改版）；换别的服务或按内容搜", stage="search")
    found = [x for x in (state.get("cbirSites") or {}).get("sites") or [] if isinstance(x, dict) and str(x.get("url", "")).startswith(("http://", "https://"))]
    sites = [{"title": str(x.get("title") or "")[:160], "url": str(x["url"])[:500], "domain": str(x.get("domain") or "")[:80],
              "size": str(x.get("description") or "")[:40]} for x in found[:MAX_SITES]]
    tags = [str(t.get("text"))[:60] for t in (state.get("cbirTags") or {}).get("tags") or [] if isinstance(t, dict) and t.get("text")][:10]
    return {"sites": sites, "tags": tags, "total_sites": len(found),
            "note": ("网站标题里常带角色名、画师、作品 tag，多个来源一致才可信；用 fetch_page 读出处确认（成人站点可以跳过）"
                     if sites else "Yandex 没找到含这张图的网站；用 inspect_image 看图再按内容搜")}


def page_state(page):
    """结果页里带 initialState 的那段 data-state（HTML 转义的 JSON）。"""
    for raw in re.findall(r'data-state="([^"]*)"', page):
        if "cbirSites" not in raw and "cbirTags" not in raw:
            continue
        try:
            st = json.loads(html_lib.unescape(raw)).get("initialState")
        except (ValueError, AttributeError):
            continue
        if isinstance(st, dict):
            return st
    return None
