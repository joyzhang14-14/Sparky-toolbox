"""builder 计划的 schema（pydantic）：提案闸（builder.review_proposal）按它校验计划；规范主题 TOPICS 也在这里（builder-dev 工具取来列在描述里）。
只依赖 pydantic，不 import 平台别的模块。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NAME_RE = r"^[a-z0-9][a-z0-9-]{0,40}$"

# 规范主题：control_center/builder/guide/<主题>.md，builder 子代理用 builder_guide(主题) 查。主题 → 一行说明（builder-dev 工具的描述里逐行列出）。
TOPICS: dict[str, str] = {
    "new-tool": "新工具",
    "new-task": "新任务",
    "listen": "监听 listen.py（定义新的或引用已有的）",
    "credential-tool": "要凭证的工具",
    "docker-tool": "起一次性容器的工具（可自带 Dockerfile）",
    "gpu-tool": "在一次性 GPU 容器里跑本地模型的工具",
    "comfyui-tool": "经 ComfyUI 的 HTTP 接口出图 / 视频",
    "comfyui-assets": "给 ComfyUI 装模型 / 自定义节点",
    "model-tier": "新模型档位（models.yaml）",
    "change-tool": "改已有工具",
    "change-task": "改已有任务",
    "workflow-task": "不起 agent、按步骤在宿主上跑 py 的任务（流水线、批处理、训练）",
    "new-subagent": "新子代理（agent / generator / engine）",
    "change-subagent": "改已有子代理",
    "untrusted-tool": "读外部内容的工具（只给子代理用）",
    "retire": "下线已有工具 / 注销已有任务",
}


def topic_lines() -> str:
    return "\n".join(f"- {k}：{v}" for k, v in TOPICS.items())


class PlanTool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME_RE, description="工具名（小写字母、数字、连字符）")
    functions: str = Field(min_length=1, max_length=2000, description="有哪些函数、各做什么")
    network: bool = Field(description="要不要联网")
    deps: list[str] = Field(default_factory=list, max_length=30, description="Python 依赖，写死版本，如 requests==2.32.3；工具文件夹的 requirements.txt 要和它一致；"
                                                                             "部署时和平台的依赖一起装进它自己的依赖环境，版本不能和平台的不同（平台已装的包不用写）")
    add_to: dict[str, dict[str, Any]] = Field(default_factory=dict, description="部署成功后把这个工具加进哪些已有任务：任务名 → 工具项（一般写 {}；要给这个任务追加用法规则写 {guide: …}）；不写就不加")
    docker: dict[str, Any] | None = Field(default=None, description="要起的容器 {images, memory, cpus, pids, tmp, disk, gpu, timeout}（同 tool.yaml 的 docker）；不起就不写；tool.yaml 要写成一样")
    untrusted: bool = Field(default=False, description="输出里有外部来源的自由文本（网页、邮件、评论、RSS、文件内容…）会进到模型 → true：只给子代理用，tool.yaml 写 usable_by: subagent，"
                                                       "不能加进任务（见 builder_guide(\"untrusted-tool\")）")
    hooks: dict[str, list[dict[str, Any]]] | None = Field(default=None, description="工具的 hooks（同 tool.yaml：事件 pre_call / post_call / run_start / run_end → "
                                                                                   "[{command: 工具文件夹里的脚本文件名, match?: [函数名]（只给 pre_call / post_call）, timeout?: 60s}]，"
                                                                                   "以 sparky 跑、联网同本工具）；不要就不写；tool.yaml 要写成一样，审批卡逐字列出")


class PlanTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME_RE)
    description: str = Field(min_length=1, max_length=500)
    trigger: Literal["schedule", "manual", "listen"]
    schedule: str | None = Field(default=None, description="cron（分 时 日 月 周，本机时区）；trigger 是 schedule 时必填")
    listen: str | None = Field(default=None, description="监听名（如 news）：同时写 listen_py = 定义新监听，任务做成文件夹 tasks/<名>/task.yaml + listen.py；"
                                                         "不写 listen_py = 引用已有的同名监听（别的任务定义的，check 触发时一起触发），任务文件夹里不放 listen.py；"
                                                         "只给 trigger: listen 的任务，且必填（见 builder_guide(\"listen\")）")
    listen_events: list[str] = Field(default_factory=list, max_length=10, description="订阅的事件来源名（如 [message]：Joy 发来的消息；这个来源的每条事件都调 on_event）；不订阅就不写")
    listen_py: str | None = Field(default=None, max_length=20000, description="定义新监听时 listen.py 的完整内容（引用已有监听不写）：部署时 tasks/<名>/listen.py 要和这里一模一样，审批卡上逐字列出")
    priority: int = Field(ge=1, le=9)
    runtime: str | None = Field(default=None, description="agent 任务必填：主模型的服务栈；workflow 任务不写（模型写在步骤里）")
    model: str | None = Field(default=None, description="agent 任务必填：主模型；workflow 任务不写")
    enabled_after_deploy: bool = Field(description="试跑成功后要不要上线")
    tools: dict[str, dict[str, Any]] = Field(default_factory=dict, description="任务 yaml 里的 tools：工具名 → 工具项（一般写 {}，写成 - 名字；要追加本任务的用法规则写 {guide: …}）；"
                                                                                "yaml 必须和这里一模一样；workflow 任务不写")
    subagents: dict[str, dict[str, Any]] = Field(default_factory=dict, description="任务 yaml 里的 subagents：子代理名（已有的，或本计划 subagents 新建的）→ 子代理项"
                                                                               "（一般写 {}，写成 - 名字；要追加本任务的规则写 {guide: …}）；不用子代理就不写；yaml 必须和这里一模一样")
    memory: list[str] = Field(default_factory=list, description="要读的记忆文件：每次运行把这些文件（文件或文件夹，文件夹 = 那一层所有 .md；~ 开头）全文注入（只读），任务 yaml 的 memory 写成一样；不读就不写")
    steps: list[list[Any]] | None = Field(default=None, description="workflow 任务：每步 [编号, 任务文件夹里的 py 文件名, 要不要资源]，要模型的加第 4 项 {runtime, model}"
                                                                    "（见 builder_guide(\"workflow-task\")）；yaml 必须和这里一模一样；agent 任务不写")
    network: bool | list[int] | None = Field(default=None, description="listen.py / workflow 的步骤要不要联网（不写 = 不联网；端口列表 = 只连本机这些端口）")
    deps: list[str] = Field(default_factory=list, max_length=30, description="workflow 任务的 Python 依赖，写死版本（同工具的 deps）：任务文件夹的 requirements.txt 要和它一致，"
                                                                         "部署时和平台的依赖一起装进 data/runtime/venvs/tasks/<名>，步骤用它的 python 跑；不要就不写")
    timeout: str | int | None = Field(default=None, description="任务 yaml 的 timeout（agent 任务：主模型时间上限；workflow 任务：各步骤在跑的时间合计），如 10m、2h、none；"
                                                                "写了 yaml 要一样，不写由开发时定")
    resumable: bool | None = Field(default=None, description="只给 workflow 任务（默认 true：要资源的步骤能在中间被抢占）；false = 步骤中间不打断，见 builder_guide(\"workflow-task\")；"
                                                             "写了 yaml 要一样，不写 yaml 也不写")
    hooks: dict[str, list[dict[str, Any]]] | None = Field(default=None, description="任务的 hooks（同任务 yaml：事件 run_start / run_end / pre_call / post_call → "
                                                                                   "[{command: 任务文件夹里的脚本文件名, match?: [函数名或工具名]（只给 pre_call / post_call）, timeout?: 60s}]，"
                                                                                   "以 sparky 跑、联网同 network）；不要就不写；yaml 要写成一样，审批卡逐字列出")

    @model_validator(mode="after")
    def _listen_matches_trigger(self) -> "PlanTask":
        if (self.trigger == "listen") != (self.listen is not None):
            raise ValueError("trigger 是 listen 时必须写 listen（监听名）和 listen_py；别的触发方式不写 listen")
        if self.listen_py is not None and self.listen is None:
            raise ValueError("listen_py 要和 listen（监听名）一起写；引用已有监听只写 listen")
        if self.listen_events and self.trigger != "listen":
            raise ValueError("listen_events 只给 trigger: listen")
        if self.steps is None:
            if self.runtime is None or self.model is None:
                raise ValueError("agent 任务要写 runtime 与 model（主模型的档位）")
            if self.resumable is not None:
                raise ValueError("resumable 只给 workflow 任务（写了 steps 的）：agent 任务一律可被抢占、从检查点续跑，不写它")
        elif self.runtime is not None or self.model is not None or self.tools or self.subagents or self.memory:
            raise ValueError("workflow 任务（写了 steps）不写 runtime / model / tools / subagents / memory：要模型的步骤把档位写在第 4 项")
        return self


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME_RE, description="档位名（models.yaml 里的键，不能和已有档位重名）")
    profile: dict[str, Any] = Field(description="完整档位，字段同 models.yaml（stb schema models）：runtime、model_path、endpoint 等，"
                                                "必须写 start / stop（启动 / 停止命令）和 min_avail_gb（占用内存 + 余量，GiB）")


class PlanToolChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME_RE, description="要改的已有工具（tool.yaml 的 is_editable 不是 false 才行）")
    change: str = Field(min_length=1, max_length=2000, description="改什么、为什么（审批卡上给 Joy 看）；能改它文件夹 tools/<名>/ 里的文件")
    fields: dict[str, Any] = Field(default_factory=dict, description="要改的 tool.yaml 声明：键 → 新值（null = 删掉这个键），审批卡逐项列出旧值 → 新值；"
                                                                     "能写 network、docker、is_editable、hooks；usable_by 不能改；description 直接改文件、不用写")
    deps: list[str] | None = Field(default=None, max_length=30, description="改依赖时写 tools/<名>/requirements.txt 的完整新内容（写死版本，同新工具的 deps；[] = 删掉这个文件，改用平台的 venv）：部署时按它重建依赖环境 data/runtime/venvs/tools/<名>；不写 = requirements.txt 不能动")


class PlanTaskChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME_RE, description="要改的已有任务")
    change: str = Field(min_length=1, max_length=2000, description="改什么、为什么（审批卡上给 Joy 看）；能改它文件夹 tasks/<名>/ 里的文件")
    tools_add: dict[str, dict[str, Any]] = Field(default_factory=dict, description="新加的工具（或改工具项的工具）：工具名 → 工具项（一般写 {}；追加用法规则写 {guide: …}）；等于加权限，卡上逐个列出")
    tools_remove: list[str] = Field(default_factory=list, description="去掉的工具名")
    subagents_add: dict[str, dict[str, Any]] = Field(default_factory=dict, description="新加的子代理（或改子代理项的）：子代理名 → 子代理项（一般写 {}；追加本任务的规则写 {guide: …}）；卡上逐个列出")
    subagents_remove: list[str] = Field(default_factory=list, description="去掉的子代理名")
    memory: list[str] | None = Field(default=None, description="改读的记忆文件：新的完整清单（[] = 不读）；不写 = 不变")
    listen_py: str | None = Field(default=None, max_length=20000, description="listen.py 的新完整内容（写了 = 这个任务定义它的监听，写在任务文件夹 tasks/<名>/listen.py）；不写 = 不变；审批卡上逐字列出")
    fields: dict[str, Any] = Field(default_factory=dict, description="要改的任务 yaml 项：键 → 新值（null = 删掉这个键），审批卡逐项列出旧值 → 新值；"
                                                                     "能写 trigger、schedule、enabled、priority（1–9，默认由 Joy 定，要有充分理由）、listen、listen_events、"
                                                                     "steps、network、hooks（workflow 的步骤与任务的 hook 一律以 sparky 跑）、"
                                                                     "runtime、model（agent 任务换主模型：两项合起来要对上一个档位）、timeout、resumable（只给 workflow 任务）；"
                                                                     "tools 用 tools_add / tools_remove，subagents 用 subagents_add / subagents_remove，memory 用 memory；"
                                                                     "system_prompt、description 这类直接改文件、不用写")
    deps: list[str] | None = Field(default=None, max_length=30, description="改依赖时写 tasks/<名>/requirements.txt 的完整新内容（写死版本，同新任务的 deps；[] = 删掉这个文件，改用平台的 venv）：部署时按它重建依赖环境 data/runtime/venvs/tasks/<名>；不写 = requirements.txt 不能动")


class PlanSubagent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME_RE, description="子代理名 = 文件夹名 subagents/<名>/ = 主模型看到的调用名；不能和已有子代理、工具重名")
    config: dict[str, Any] = Field(description="subagents/<名>/subagent.yaml 的全部内容（字段见 builder_guide(\"new-subagent\") 与 stb schema subagents）：部署时要和这里一模一样；"
                                               "不写 isolated（隔离子代理只由 Joy 建）、不写 is_editable: false")
    files: dict[str, str] = Field(default_factory=dict, description="文件夹里 yaml 以外的文件：文件名 → 做什么（server 的程序、guide.md、output.py、hooks 的脚本）；"
                                                                 "审批卡逐个列出，部署时文件夹里正好是这些文件加 yaml")


class PlanSubagentChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME_RE, description="要改的已有子代理（subagents/<名>/，yaml 的 is_editable 不是 false 才行）")
    change: str = Field(min_length=1, max_length=2000, description="改什么、为什么（审批卡上给 Joy 看）")
    fields: dict[str, Any] = Field(default_factory=dict, description="要改的 yaml 字段：键 → 新值（null = 删掉可选字段），审批卡逐项列出旧值 → 新值；"
                                                                     "name、is_editable、isolated 不能改")
    files: dict[str, str] = Field(default_factory=dict, description="要新建 / 改 / 删的文件夹里 yaml 以外的文件：文件名 → 改什么；没列的文件部署时必须原样不变")


class PlanRetireTool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME_RE, description="要下线的已有工具（is_editable 不是 false 的）")
    reason: str = Field(min_length=1, max_length=1000, description="为什么下线（审批卡上给 Joy 看）")
    remove_from: list[str] = Field(default_factory=list, max_length=20, description="还在用它、要从 tools 里撤掉它的已有任务名（合入提交里改这些任务 yaml，只删这一项）；"
                                                                                  "一起注销的任务不用写")


class PlanRetireTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=NAME_RE, description="要注销的已有任务（部署时 stb task rm --keep-data：注销登记、删定义；运行记录与工作目录保留）")
    reason: str = Field(min_length=1, max_length=1000, description="为什么注销（审批卡上给 Joy 看）")


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    purpose: str = Field(min_length=1, max_length=2000, description="要解决什么问题")
    tools: list[PlanTool] = Field(default_factory=list, max_length=3)
    tasks: list[PlanTask] = Field(default_factory=list, max_length=2)
    change_tools: list[PlanToolChange] = Field(default_factory=list, max_length=3, description="修改已有的工具（is_editable 不是 false 的）")
    change_tasks: list[PlanTaskChange] = Field(default_factory=list, max_length=2, description="修改已有的任务：工具增减、读记忆、fields 里逐项写的触发方式 / 步骤声明等")
    models: list[PlanModel] = Field(default_factory=list, max_length=2, description="新工具要跑新模型时的新档位：Joy 批准后，部署时（合入之后）平台写进 models.yaml")
    subagents: list[PlanSubagent] = Field(default_factory=list, max_length=2, description="新子代理（subagents/<名>/ 文件夹，见 builder_guide(\"new-subagent\")）")
    change_subagents: list[PlanSubagentChange] = Field(default_factory=list, max_length=2, description="修改已有子代理（is_editable 不是 false 的，见 builder_guide(\"change-subagent\")）")
    retire_tools: list[PlanRetireTool] = Field(default_factory=list, max_length=5, description="下线已有工具：从各任务的 tools 里撤掉（remove_from），删掉它的文件（合入提交里的删除）")
    retire_tasks: list[PlanRetireTask] = Field(default_factory=list, max_length=5, description="注销已有任务：删掉它的定义文件（合入提交里的删除），部署时 stb task rm")
