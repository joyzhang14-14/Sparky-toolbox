## workflow 任务（workflow-task）
不起 agent，按步骤在宿主上跑任务文件夹里的 py：流水线、批处理、导出、训练。不进容器，GPU 直接用。task.yaml 写法需要时读 control_center/builder/examples/workflow-task-task.yaml，一步的 py 写法读 control_center/builder/examples/workflow-task-step.py。
- steps：每步一行 [编号, 文件, 要不要资源]，编号从 1 起按顺序；文件是任务文件夹里的 .py 文件名（不带路径）。
  - 要 GPU、大内存或模型的步骤写 true：排队等机器，轮到了才跑。要平台起好的模型服务（如 vLLM）的加第 4 项 {runtime, model}（models.yaml 档位的 runtime + model_path，新服务见 model-tier），连接信息在环境变量 STB_MODEL（JSON：endpoint、call_name…）；不写第 4 项 = 不起模型服务，平台停掉所有模型、等内存够了再跑（训练、自己加载模型的脚本）。不同步骤可以用不同模型。
  - 只读写文件、联网的步骤写 false：马上跑，不排队。
- 每一步以 sparky 跑，工作目录是任务文件夹；能 import control_center，凭证用 credential("KEY")。环境：STB_RUN_DIR（本次运行给步骤的可写目录）、STB_TASK、STB_STEP（第几步）、STB_REPLY_FILE、重跑时 STB_RESUME=1；触发方给的内容在 $STB_RUN_DIR/prompt.txt（有才有）。步骤之间用 $STB_RUN_DIR 或任务工作区的文件交接。输出进运行目录的 step-<编号>.log。network 不写就不能联网（连本机端口写端口列表）。
- 退出码：0 = 这步完成；3 = 没事可做，运行到此结束、不发消息；别的 = 失败（Joy 收到错误摘要和日志末尾）。
- 给 Joy 的消息：把要发的文字写进 $STB_REPLY_FILE，运行完成时原样发出；没写就发「✅ <任务> 完成」。
- 要资源的步骤跑到一半可能收到 SIGTERM（让位给更高优先级的任务）：20 分钟内把进度存进任务工作区、以退出码 75 退出，之后这一步重跑（STB_RESUME=1），从存档接着；没处理 SIGTERM 的，这一步之后从头重跑。所以每一步都要能重跑（重跑不重复产出、不弄坏数据）。存不了进度、又不能重跑的，任务写 resumable: false（步骤中间不打断）。不要资源的步骤不会被打断，步骤全是 false 的任务不写 resumable。
- 依赖：要别的包就在计划任务的 deps 里写（写死版本，平台已装的包不用写），任务文件夹放一模一样的 requirements.txt，部署时装进它自己的依赖环境，步骤用它的 python 跑；开发区里装在 /deps/tasks/<名>。
  离线 wheel 装不了的（如要源码编译 CUDA 的训练栈）用 ask_user 请 Joy 手工建在 /home/joyzhang14/apps/<名>/，步骤的 py 用 subprocess 调那个 python 的绝对路径；不在任务文件夹里建 .venv。改已有任务的依赖见 change-task。
- 输出：一律写平台文件夹外的绝对路径，不用 ~：检查点、数据集、中间数据、生成的文件写任务工作区 /home/sparky/workspace/<任务>/；研究报告与数据写 /home/joyzhang14/research/<项目>/；训练出的模型写 /home/joyzhang14/models/…。写不进去的目录用 ask_user 请 Joy 开权限。
- 要先判断有没有活再跑：写 trigger: listen，用 listen.py 的 check(ctx) 判断（见 listen）。
- 计划里 steps、network 要和任务 yaml 一模一样。
- 验证：每一步的逻辑在开发区用单测覆盖（临时目录当工作区，STB_RUN_DIR、STB_REPLY_FILE 指向临时文件），不用 shell 在宿主上真跑步骤、清理产物（这些会改文件，每条都要 Joy 批卡；只读核对可以用 shell）；真跑交给部署时的试跑。
- 交回总结里写上手动跑法 /home/joyzhang14/sparky-toolbox/.venv/bin/stb run --now <名>（enabled: false 也能跑）和日志位置 /home/joyzhang14/sparky-toolbox/logs/runs/<名>/<运行时间>/step-<编号>.log（发给 Joy 的文字在同目录 steps/reply.txt）。
常见错误：要模型或 GPU 的步骤写成 false；检查点写进没有写权限的目录；输出写成相对路径或用了 ~；在任务文件夹里建 .venv；用 PYTHONPATH / sys.path import 别的文件夹的代码；把凭证写进文件。
