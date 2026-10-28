"""以 sparky 起工具 / 监听 / 任务命令 / hook 的启动器（launch.sparky_command 用 sudo -u sparky 执行本文件）。

- 配置从环境变量 STB_LAUNCH 读（JSON：{deny_tcp, ports?, code, server} 或 {deny_tcp, ports?, exec}），读完就从环境里删掉；
  argv[1] 只是给日志看的名字（sudo 把命令行记进 journald：命令行里只有本文件路径和这个名字，不放源码与配置）。
- 先按 network 上 Landlock（control_center/landlock.py，只限联网、不管文件），再设不可 dump，然后 exec bash -c <命令>，或 runpy 跑服务器脚本。
  文件读写看 sparky 用户本身的文件权限。
- 本文件与 landlock.py 都在仓库里，sparky 改不了。
"""
import importlib.util
import json
import os
import runpy
import sys

_here = os.path.dirname(os.path.abspath(__file__))
sys.path[0] = ""             # 同 python -c：不把 control_center/ 放进 sys.path（里面的 env.py、paths.py 等会遮住同名模块）
_spec = importlib.util.spec_from_file_location("_stb_landlock", os.path.join(_here, "landlock.py"))
_ll = importlib.util.module_from_spec(_spec)  # pyright: ignore[reportArgumentType]
_spec.loader.exec_module(_ll)  # pyright: ignore[reportOptionalMemberAccess]
_c = json.loads(os.environ.pop("STB_LAUNCH"))
_ll.restrict(deny_tcp=_c["deny_tcp"], ports=_c.get("ports", []))
_ll._libc.prctl(4, 0, 0, 0, 0)   # PR_SET_DUMPABLE 0：/proc/<pid>/environ 等归 root，别的 sparky 进程（如 shell）读不到环境里的凭证
os.umask(0o002)              # 新文件组可写：sparky 的文件归 sparkle 组（Joy 与 sparky 共用）
if "exec" in _c:             # 命令（任务的 command、以 sparky 跑的 hook）：联网限制已生效，换成 bash 跑
    os.execvp("bash", ["bash", "-c", _c["exec"]])
del _here, _spec, _ll
sys.path.insert(0, _c["code"])
sys.argv = [_c["server"]]
runpy.run_path(_c["server"], run_name="__main__")
