# shell 用法

先想能不能不用它：只是读文件、列目录、grep、在沙箱里能算的，用沙箱里的 read / ls / grep / find / bash；shell 只用于要联网、要写 Joy 的家目录、要 GPU 或看本机进程的事。

参数：
- command：命令（完整 bash 语法）。
- description：一句话说清这条命令在做什么（如「列出磁盘占用」）；必填。
- timeout 可选：秒数或 90s / 5m（不写或 0 = 默认 600 秒，不设上限；慢的命令写大一点）。

能合并的检查写成一条（用 ; 或 && 连）。
每条命令先试跑：不改文件、不联网、不连本机服务（Unix socket）、不给别的进程发信号、不用 GPU、内存不超过 4 GB 的，不发卡，直接在只读环境里执行，结果开头标「只读，没发卡」；
其余的发审批卡（卡上列出试跑看到的效果），Joy 批准才执行。所以只读检查和要改东西的操作分开写：检查不用等卡。
返回退出码与输出（stdout+stderr，过长会截断）。被拒绝时结果里有 Joy 的意见，照他说的改。
结束进程：不要按名字 / 模式杀（pkill、killall、kill $(pgrep …)），先 pgrep -af <关键词> 列出候选，确认后按 PID kill。

运行环境：
- 在 /home/sparky 跑，~ 就是 /home/sparky。平台仓库在 /home/joyzhang14/sparky-toolbox（工具 tools/、任务 tasks/、子代理 subagents/ 各一个文件夹；只读，git 要加 -c safe.directory='*'）；stb 命令不在 PATH 里，查有哪些工具 / 任务就 ls 这几个文件夹。
- 不要用 sudo，docker。
- 能读 Joy 的家目录 /home/joyzhang14（凭证除外）；能写 /home/sparky、/tmp、整个 /home/joyzhang14/Obsidian、/home/joyzhang14/apps、/home/joyzhang14/models、/home/joyzhang14/research/tenbagger、平台为工具开的目录（本次运行的工作 / 运行目录等）。
