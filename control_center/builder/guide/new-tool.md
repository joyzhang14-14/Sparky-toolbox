## 新工具（new-tool）
文件夹 tools/<名>/（名字不能和已有工具、子代理重名）：server.py、tool.yaml、requirements.txt（有依赖才写）、guide.md（可选：模型第一次调用时先给它的用法规范）、install.py（可选：部署时合入前跑一次，失败要自己撤回、退出码非 0）；测试写 tests/stb_platform/test_<名>.py。
写法需要时读 control_center/builder/examples/new-tool-server.py、control_center/builder/examples/new-tool-tool.yaml。
- 运行：可写目录是 STB_STATE_DIR、STB_MEDIA_DIR、STB_TOOL_RUN_DIR；network: false 时不能联网。
- 平台模块（control_center）：tool_kit（报错标记与审批卡）、credential（凭证）、media（成品目录与 GPU 容器）、docker_client（起容器）。
- tool.yaml 字段：name（= 文件夹名）、description、usable_by（见 untrusted-tool）、is_editable、network、docker、hooks（pre_call / post_call / run_start / run_end，本文件夹里的脚本，联网同本工具）。network、docker、usable_by、hooks 必须和计划一样（计划 untrusted: true 对应 usable_by: subagent；计划没写的不写）。network 只在运行时真要联网才写 true。
- 依赖：requirements.txt 一行一个 包==版本，和计划的 deps 一模一样；平台已装的包不用写，写了版本也不能和平台的不同。
- 没有作用域：数量、时长、时间窗这类写成函数参数（带默认值）；某个任务要的限制写进那个任务的 system_prompt。
- 少用的、要审批的参数默认不填，用法说明里写「平常不要写」；不要设计成一填就触发大额申请。
- 函数名一律 <工具名>_<动作>；出错 raise ToolError(原因)。
- 成品写进 STB_MEDIA_DIR，文件名用 media.next_name(种类) 起（固定文件名在同一次运行里会互相覆盖），返回成品路径（media.sandbox_path：沙箱里外同一个路径）。参数里的路径用 media.host_path 校验（要在工作目录里）。
- STB_STATE_DIR 只放工具自己的状态（已读位置、缓存、登录信息）；要长留的报告与数据写 /home/joyzhang14/research/<项目>/，模型写 /home/joyzhang14/models/…。
- 运行信息（环境变量）：
  - STB_WORK_ROOT：任务的工作目录（沙箱里外同一个路径）。
  - STB_RUN_ID、STB_RUN_REF（<任务>/<运行 id>）：起容器时必须带 STB_RUN_REF。
  - STB_PAUSE_FILE：这个文件出现就尽快停手（长活要看它）。
  - STB_TOOL_RUN_DIR：本次运行这个工具的目录。
  - STB_RUN_IN：本次运行的附件目录；只给要读 Joy 附件原文的工具，这类工具要标 untrusted。
  - STB_RUN_DIR：运行目录，放排查记录；里面的内容不要放进返回值。
- 已读位置：起点读 STB_STATE_DIR/<键>.json，新位置写进 STB_TOOL_RUN_DIR/<键>.json，运行 complete 时由工具自己的 run_end hook 写回；照 tools/read-email/ 的 cursor_hook.py 与 _cursor.py 复制进自己文件夹。
- 读新邮箱不做新工具：在 tools/read-email/mailboxes/ 加一个 <键>.py（写法见同文件夹的 server.py 开头），不改别的文件。
- 凭证见 credential-tool。
- 测试：import tools/<名>/ 下的模块直接调函数，网络与外部调用用 monkeypatch 换掉。
常见错误：requirements.txt 和计划的 deps 不一致（或依赖写进了 tool.yaml）；函数名没带前缀；写了没有写权限的路径；用了计划 deps 里没有的包。
