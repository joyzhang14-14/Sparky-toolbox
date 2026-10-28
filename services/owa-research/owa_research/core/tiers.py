"""来源可信度分级：不用模型的域名规则。

1 official   官方 / 一手（政府、学术机构、标准组织、期刊、监管申报、奖项官方站）
2 reference  权威参考与主流媒体（百科、通讯社、大报、大型科学媒体、代码托管的项目主页）
3 other      其它（默认）
4 low        社交、问答、内容农场——只当线索，需在别处确认
"""
from __future__ import annotations

from urllib.parse import urlsplit

TIER_LABEL = {1: "official", 2: "reference", 3: "other", 4: "low"}
TIER_BONUS = {1: 0.15, 2: 0.08, 3: 0.0, 4: -0.3}  # 叠加在重排分数上的排序加权

_T1_SUFFIX = (".gov", ".edu", ".mil", ".int", ".go.jp", ".ac.jp", ".ac.uk", ".gov.uk", ".gov.cn", ".edu.cn", ".gov.au", ".edu.au", ".gc.ca", ".europa.eu", ".gov.sg", ".gov.hk")
_T1_HOSTS = {"sec.gov", "iau.org", "minorplanetcenter.net", "who.int", "un.org", "imf.org", "worldbank.org", "oecd.org", "nobelprize.org", "improbable.com",
             "arxiv.org", "doi.org", "nature.com", "science.org", "ncbi.nlm.nih.gov", "ieee.org", "acm.org", "nasa.gov", "esa.int", "stat.go.jp", "iso.org", "ietf.org", "w3.org"}
_T2_HOSTS = {"wikipedia.org", "wikidata.org", "britannica.com", "reuters.com", "apnews.com", "bbc.co.uk", "bbc.com", "nytimes.com", "wsj.com", "ft.com", "bloomberg.com",
             "theguardian.com", "economist.com", "scientificamerican.com", "newscientist.com", "space.com", "github.com", "gitlab.com", "cnn.com", "npr.org", "nikkei.com",
             "nhk.or.jp", "xinhuanet.com", "people.com.cn", "sciencedirect.com", "springer.com", "wiley.com", "jstor.org", "archive.org", "phys.org", "eurekalert.org"}
_T4_HOSTS = {"reddit.com", "quora.com", "pinterest.com", "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com", "medium.com", "blogspot.com",
             "wordpress.com", "answers.com", "brainly.com", "wikihow.com", "fandom.com", "tumblr.com", "youtube.com", "zhihu.com", "baidu.com", "tieba.baidu.com"}


def _host_in(host: str, hosts: set[str]) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def tier_of(url: str) -> int:
    host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    if not host:
        return 3
    if _host_in(host, _T4_HOSTS):
        return 4
    if _host_in(host, _T1_HOSTS) or host.endswith(_T1_SUFFIX):
        return 1
    if _host_in(host, _T2_HOSTS):
        return 2
    return 3


def tier_label(url: str) -> str:
    return TIER_LABEL[tier_of(url)]
