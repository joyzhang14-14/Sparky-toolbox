"""docker 工具的 server.py 示范：用平台的 control_center/docker_client.run 起一次性容器（用法同 subprocess.run）。
tool.yaml 的 docker 段见 docker-tool-tool.yaml；example 换成工具名。"""
import os
import subprocess
import time

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from control_center import docker_client
from control_center.media import host_path, media_dir, next_name, sandbox_path
from control_center.tool_kit import platform_fault, timeout_note, tool_timeout

TOOL = "example"
IMAGE = "stb-example"                         # 要在 tool.yaml 的 docker.images 里
server = MCPServer(TOOL)


@server.tool(name="example_convert", description="把工作目录下的一个文件转换成 PNG，存到工作目录 media/<运行>/")
def example_convert(path: str, timeout: int = 300) -> str:
    src = host_path(path)                     # 模型写的绝对路径（沙箱里外同一个），要在工作目录里
    if not src.is_file():
        raise ToolError(f"文件不存在：{path}")
    out = media_dir()                         # 本次运行的成品目录（STB_MEDIA_DIR）
    name = next_name("image") + ".png"
    mounts = [{"host": str(src.parent), "container": "/in", "mode": "ro"},    # 只能挂工具本来就能碰的目录
              {"host": str(out), "container": "/out", "mode": "rw"}]
    t0 = time.monotonic()
    try:
        r = docker_client.run(IMAGE, ["convert", f"/in/{src.name}", f"/out/{name}"], tool=TOOL, run=os.environ["STB_RUN_REF"],
                              mounts=mounts, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise tool_timeout(timeout_note("转换", time.monotonic() - t0, timeout, True)) from None
    except OSError as e:                      # 服务不在、请求被拒（docker_client.DockerError）
        raise platform_fault(f"容器没起来：{e}") from None
    if r.returncode == 137:
        raise platform_fault("容器被强制终止（退出码 137，多半是内存不够）")
    if r.returncode != 0:
        raise ToolError(f"转换失败（退出码 {r.returncode}）：{r.stderr.strip()[-300:]}")
    return sandbox_path(out / name)           # 返回成品路径（工作目录 media/<运行 id>/<文件名>）


if __name__ == "__main__":
    server.run("stdio")
