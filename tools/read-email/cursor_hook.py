"""read-email 的 run_end hook（tool.yaml 的 hooks 登记）：运行 complete 时，把每个邮箱本次从第 1 封起连续被成功调用交出去的部分写回 data/tool_state/read-email/；
有取信失败的调用、这批没全交出去再输出改判 failed（说明哪个邮箱没读完）。写法与约定见同文件夹的 _cursor.py，邮箱在 mailboxes/（server.py 载入）。"""
import sys

import _cursor
from server import MAILBOXES

if __name__ == "__main__":
    sys.exit(_cursor.main_run_end("read-email", {k: v.NAME for k, v in MAILBOXES.items()}))
