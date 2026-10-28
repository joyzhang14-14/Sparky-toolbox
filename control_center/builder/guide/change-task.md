## 改已有任务（change-task）
计划 change_tasks 每项：name、change（改什么、为什么）、tools_add（工具 → 工具项，一般 {}）、tools_remove（工具名列表）、subagents_add、subagents_remove（同 tools）、memory（新的完整清单，[] = 不读；不改不写）、fields（项 → 新值）、deps（改依赖才写）。
- 能改任务文件夹 tasks/<名>/ 里的文件：task.yaml（system_prompt、description 这类直接改文件、在 change 里说明）、workflow 的步骤 py 等；listen.py 只能按计划的 listen_py 改（见下）。
- tools：部署时必须正好等于「原来的 - tools_remove + tools_add」，工具项一模一样（工具项只有 guide）；改某个工具的工具项也写在 tools_add（写完整的新工具项）。
- subagents：同 tools，部署时必须正好等于「原来的 - subagents_remove + subagents_add」（子代理项只有 guide）；加的要是已有子代理或本计划新建的。
- fields 能写：trigger、schedule、enabled、priority、listen、listen_events、steps、network、hooks、runtime、model（只给 agent 任务）、timeout、resumable（只给 workflow 任务）；值 null = 删掉。换主模型时 runtime、model 合起来要对上能当主模型的档位；workflow 任务换模型改 steps 里那一步的第 4 项。没写进 fields 的这些项必须原样不变。
- priority（1–9；不要改优先级是 10 的任务）：优先级默认由 Joy 定，只有 Joy 明确要求或有充分理由时才提，理由写进 change。
- 新的 ${} 只能用 ${STB_OWNER_DISCORD_ID}。
- 依赖（workflow 任务）：要改 tasks/<名>/requirements.txt 就在计划写 deps = 它的完整新内容（写死版本，同 workflow-task；[] = 删掉这个文件、改用平台的 venv），文件要和 deps 一模一样；开发区里装在 /deps/tasks/<名>。没写 deps 时 requirements.txt 不能动。
- 已有任务部署后不自动试跑。
需要时读 control_center/builder/examples/change-task-plan.json。
listen_py：写了就是这个任务定义它的监听，listen.py 放任务文件夹里。
常见错误：tools_add 里抄错原工具项；任务要的限制（只读、能写哪些笔记、发几条）没写进 system_prompt；listen.py 改了却没在计划的 listen_py 里写出新内容；改了 trigger 没同时改 schedule。
