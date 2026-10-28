"""新工具 tools/<名>/server.py 示范（tool.yaml 见 new-tool-tool.yaml）。函数名一律 <工具名>_<动作>，出错 raise ToolError(原因)。"""
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

server = MCPServer("weather")


@server.tool(name="weather_today", description="今天的天气；city 不写就查 Irvine")
def weather_today(city: str = "Irvine") -> str:
    if not city.strip():
        raise ToolError("city 不能为空")
    ...


if __name__ == "__main__":
    server.run("stdio")
