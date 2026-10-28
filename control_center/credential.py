"""平台给工具的凭证接口（tool.yaml 不声明凭证，工具用到时自己读）：`from control_center.credential import credential`，credential("KEY") 从平台的 .env 取值
（解析用 control_center/env.py；以 sparky 跑的工具对 .env 只读）。缺了或是空的抛 MissingCredential：报错文字带 [缺凭证] 标记和键名，
网关据此给 Joy 发一张凭证卡（只有他能填，值经中枢内存写进 .env），告诉模型填好后再调用。"""
from __future__ import annotations

from control_center import env

MARK = "[缺凭证]"   # 同 control_center/registry.py 的 MISSING_CREDENTIAL（网关认这个标记）


class MissingCredential(Exception):
    """.env 里没有这个键（或值是空的）：str() 是「[缺凭证]<键>」，工具报错时原样到网关。"""

    def __init__(self, key: str) -> None:
        super().__init__(f"{MARK}{key}")
        self.key = key


def credential(key: str) -> str:
    """凭证 key 的值（进程环境变量优先，其次 .env）；没有或是空的抛 MissingCredential。.env 权限不对照样报错（env.EnvError）。"""
    v = env.get(key)
    if not v:
        raise MissingCredential(key)
    return v
