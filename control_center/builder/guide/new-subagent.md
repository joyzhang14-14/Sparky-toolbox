## 新子代理（new-subagent）
子代理：主模型调用时发一段 prompt（generator / engine 另传声明的 inputs），子代理用自己的模型、系统提示、工具或程序做完交回结果。
只有子代理能让平台换模型（工具不行）。一个子代理是一个文件夹 subagents/<名>/：subagent.yaml + 约定文件（guide.md、output.py、server.py 程序、install.py 安装脚本、hooks 的脚本），用它的任务在 yaml 的 subagents 里写它的名字。
三种（kind）：
- agent：语言模型带工具。写 system_prompt + tools（它能用的工具，可以是 []），在沙箱里跑，最后一条不调用工具的回复就是交回的结果，system_prompt 里要写明交回什么；上一次调用没交回就中断了的，同一次运行里再调用时接着原来的对话。
- generator：生成程序（如 ComfyUI 出图）。程序是文件夹里的 server.py，写 inputs，一定用自己的模型档位（不能 inherit），程序的结果就是交回的结果。
- engine：自带循环的程序（server.py），inputs 可写；prompt 进、结构化结果出。
计划 subagents 每项：name、config（subagent.yaml 的全部内容）、files（文件名 → 做什么）；用它的任务在 subagents 里写它（新任务的 subagents，或 change_tasks 的 subagents_add）。
subagent.yaml 写法需要时读 control_center/builder/examples/new-subagent-subagent.yaml。
- 字段规则同 stb schema subagents（按 kind 逐项查：必填的写齐、不属于这种的不写）；name = 文件夹名，不能和已有子代理、工具重名。
- 不写 isolated、is_editable: false。
- agent 的 tools 不能有读外部内容的工具（usable_by: subagent、计划 untrusted 的，见 untrusted-tool）：它们只给隔离子代理用。要读外部内容就让任务调用已有的隔离子代理。
- 子代理不能调子代理：tools 里不写子代理名。
- 程序（generator / engine 的 server.py）：用平台的 venv（不能加依赖，子代理没有 requirements.txt），可以 import mcp、平台的 control_center 和自己文件夹里的模块（参考 subagents/image/server.py 与同文件夹的 _comfy.py）。
  成品写本次运行的 media 目录（工作目录 media/<运行 id>/），只连它模型档位端点的端口；环境里有 STB_SUBAGENT_NAME、STB_WORK_ROOT、
  STB_RUN_ID、STB_RUN_DIR、STB_PAUSE_FILE、STB_RUN_IN，以及 STB_MODEL（它的模型档位在 models.yaml 里的整段配置，JSON，另加 name 与调用名 call_name；地址取 endpoint，安装目录取 home）。
  程序第一次被调用时才起，同一次运行里共用；返回文字（JSON 就原样交给主模型），图片块会附给主模型看。
  成品是图、视频这类文件时，结果附预览图（图片块：media.preview / media.result），主模型看过成品再发 Joy；只回文字路径，主模型看不到成品。
- guide.md（可选）：主模型这次运行第一次调用它时先返回、不执行——写怎么写 prompt、各个 inputs 怎么填、要多久、结果长什么样；
  有成品的还要写：怎么检查成品、什么算不合格、不合格时重试改什么（换种子、改 prompt 等）、最多重试几次、什么时候先问 Joy。
- output.py（可选）：检查 / 整理结果：stdin 给 {subagent, kind, prompt, inputs, result}；退出码 0 = 合格（stdout 写 {"result": …} 就换成它），2 = 打回重写（stderr 是原因），别的 = 出错。
- hooks（可选，同工具：pre_call / post_call / run_start / run_end，本文件夹里的脚本）。
  要换模型的子代理写 pre_call hook：换模型前查输入（文件在不在、在不在工作目录下、类型对不对、数量），不过就退出码 2 拦下、stderr 写明原因，不白卸主模型。
  经 ComfyUI 的写 post_call hook 删本次运行在 ComfyUI 里的副本（见 comfyui-tool）。
- install.py（可选）：部署时合入之前平台跑一次（联网不限，环境带 STB_MODEL），装它要的东西（如 ComfyUI 节点，见 comfyui-assets）；失败要自己撤回、退出码非 0。
- files：文件夹里 yaml 以外的每个文件都列上（文件名 → 做什么），部署时文件夹里正好是 yaml + 这些文件；generator / engine 要有 server.py。测试写 tests/stb_platform/test_<名>.py；单测过了再用 builder_try_subagent(name, prompt, inputs) 按正式调用真调一次验证。
- 模型文档：模型信息只记在 Obsidian 的 Sparky/ModelOverallStatus.md。换模型、加档位后用 ask_user 告诉 Joy 要更新什么（档位名、模型、占用内存、用途），由他更新。
常见错误：runtime / model 对不上任何档位；generator 写了 inherit；程序注册的函数名不等于子代理名；files 漏了文件或多了文件；agent 的 tools 写了读外部内容的工具。
