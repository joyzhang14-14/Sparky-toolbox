"""SauceNAO 以图搜图（官方 Search API，output_type=2 返回 JSON）：插画 / 动漫 / 游戏图找原帖与画师。相似度低于 MIN_SIMILARITY 的不要。"""
from owa_research.core.errors import ErrorCode, ToolError

NAME = "saucenao"
DESCRIPTION = ("只擅长插画、动漫、漫画、游戏 CG、同人图（pixiv、Twitter、booru 等图库），给原帖链接、作品名、画师、相似度；照片、实物、截图基本搜不到；"
               "裁得太狠、加了大量文字或重绘过的认不出；每 30 秒 4 次、每天 100 次。插画先用它")
KEY = "STB_SAUCENAO_API_KEY"
URLS = ["https://saucenao.com/search.php"]
MIN_SIMILARITY = 55.0


async def search(image, key, http):
    r = await http.post_form(URLS[0], params={"output_type": 2, "numres": 8, "db": 999, "api_key": key},
                             files={"file": (f"{image.id}.jpg", image.data, image.mime)})
    if r.status_code == 429:
        raise ToolError(ErrorCode.RATE_LIMITED, "SauceNAO 额度用完（每 30 秒 4 次、每天 100 次）；改用 inspect_image 看图再按内容搜", stage="search")
    try:
        data = r.json()
    except ValueError:
        raise ToolError(ErrorCode.SEARCH_UNAVAILABLE, f"SauceNAO 返回 {r.status_code}，不是 JSON", stage="search") from None
    status = int((data.get("header") or {}).get("status", -1))
    if status != 0:
        raise ToolError(ErrorCode.SEARCH_UNAVAILABLE, f"SauceNAO 状态 {status}：{str((data.get('header') or {}).get('message', ''))[:150]}", stage="search")
    sites = []
    for x in data.get("results") or []:
        h, dd = x.get("header") or {}, x.get("data") or {}
        sim = float(h.get("similarity") or 0)
        if sim < MIN_SIMILARITY:
            continue
        urls = [u for u in (dd.get("ext_urls") or []) if isinstance(u, str) and u.startswith(("http://", "https://"))][:3]
        if isinstance(dd.get("source"), str) and dd["source"].startswith(("http://", "https://")):
            urls.append(dd["source"])
        sites.append({"url": urls[0] if urls else "", "urls": urls[:4], "similarity": sim, "index": str(h.get("index_name") or "")[:80],
                      "title": str(dd.get("title") or dd.get("eng_name") or dd.get("jp_name") or "")[:200],
                      "author": str(dd.get("member_name") or dd.get("creator") or dd.get("author_name") or dd.get("author") or "")[:120],
                      "characters": str(dd.get("characters") or "")[:200], "material": str(dd.get("material") or "")[:200]})
    return {"sites": sites[:8], "tags": [],
            "note": ("用 fetch_page 读出处网页；要原图就对读过的网页调 page_images 再 view_images" if sites else
                     f"没有相似度 ≥ {MIN_SIMILARITY:.0f} 的结果（SauceNAO 只擅长插画 / 动漫 / 游戏图）；换别的服务，或用 inspect_image 看图再按内容搜")}
