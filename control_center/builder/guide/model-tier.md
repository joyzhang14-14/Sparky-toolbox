## 新模型档位（model-tier）
新工具 / 新任务要跑 models.yaml 里没有的模型时，计划的 models 每项：name（档位名）、profile（完整档位）。已有档位计划不能改。
profile 字段同 models.yaml（stb schema models）：
- 都要：runtime、start、stop（启动 / 停止命令，不经 shell，第一个词可以 ~/ 开头）、min_avail_gb（加载前要求的 MemAvailable = 实测占用 + 余量，GiB）。
- 服务档位还要：model_path、endpoint。端点答 /models 时，root 等于 model_path 的那一条就是这个档位；端点不答 /models 时回 HTTP 200 就算在跑（宿主程序）。可选 vision、container、home（程序的安装目录，python 用 <home>/venv/bin/python）。
- 要当 agent 任务的主模型还要：context_window（上下文长度）。
- 起命令之后平台最多等 600 秒就绪。
需要时读 control_center/builder/examples/model-tier-profile.json。
- 给 workflow 任务的步骤起它要连的服务也用服务档位（步骤不能调 docker）：start / stop 写 docker 命令（不经 shell，路径写绝对路径，不展开 $HOME），
  步骤的第 4 项 {runtime, model} 指向它（写法见 workflow-task）。
服务档位需要时读 control_center/builder/examples/model-tier-service.json。
规则：档位名不能和已有的重；runtime + model_path 不能和已有档位相同；agent 任务或 workflow 的步骤用它就写 runtime + model（= model_path）。启停脚本本身不在仓库里，计划里写清楚它在哪、做什么。
- 模型文档：模型信息只记在 Obsidian 的 Sparky/ModelOverallStatus.md。换模型、加档位后用 ask_user 告诉 Joy 要更新什么（档位名、模型、占用内存、用途），由他更新。
常见错误：漏 stop 或 min_avail_gb；min_avail_gb 写成占用本身（没留余量）；字段名写错（报「未知字段」）。
