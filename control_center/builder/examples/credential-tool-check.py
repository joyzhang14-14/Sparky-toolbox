"""要凭证的工具：<工具名>_check 示范（server.py 里的一个函数）。凭证只在用到时 credential("KEY") 读，不在模块顶层读。"""
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from control_center.credential import credential

server = MCPServer("weather")
URL = "https://api.example.com/v1/current"


@server.tool(name="weather_check", description="验证 API key")
def weather_check() -> str:
    r = httpx.get(URL, params={"key": credential("STB_WEATHER_KEY"), "q": "Irvine"}, timeout=20)
    if r.status_code != 200:
        raise ToolError(f"key 不能用：HTTP {r.status_code}")
    return "ok"
