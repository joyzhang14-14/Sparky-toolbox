## builder 怎么用（提案格式）

调用 builder 时 prompt 只写一份 YAML 提案（可以放在 ```yaml 围栏里，围栏外不写别的：卡上只显示提案，要 Joy 看到的说明写进 purpose），顶层正好写下面两种之一（oneoff: 或 plan:）。平台先校验，再请 Joy 批准：

- 开发计划（plan:）：结果交回给你。
- 一次性任务（oneoff:）：这次调用返回「已建好并入队」，结果由那个任务发给 Joy。
- 提案不合格：返回哪里不对、改法和对应规范，照改再调用。Joy 拒绝且写了意见：照意见改提案再调用；没写意见=不做了。
- 同一份已批准的计划还没部署完时，再用同样的提案调用就接着开发。
- 搭建需求，关键选择没说清时，先用 ask-user 的填空题问清再写提案。

完整示例（需要时自己读）：/home/joyzhang14/sparky-toolbox/control_center/builder/examples/proposal-oneoff.yaml、/home/joyzhang14/sparky-toolbox/control_center/builder/examples/proposal-plan.yaml。

### 一次性任务 oneoff:（长活交给后台跑一次，不在私信里硬做；只能用已有工具和子代理）
- name：短名（平台加前缀 oneoff-）。
- purpose：目的（给 Joy 看）。
- system_prompt：给后台 agent 的完整任务说明（它看不到这次私信，要写全）。
- tools：已有工具名 → 工具项（一般 {}）。要私信 Joy 结果就列 reply，要中途问 Joy 就列 ask-user。要读写文件、跑不联网的脚本（含 ffmpeg 等沙箱共用环境里的程序）就列 workspace（在断网沙箱里，不用审批）；shell 只读的命令直接执行、不发卡；会改文件、要联网、要 GPU 的每条都要 Joy 批，只用在必须在宿主上跑的事（要联网、要宿主上的浏览器或渲染）。
- subagents：要用的已有子代理名 → 子代理项（一般 {}），如画图 image、做视频 video、联网研究 isolated-web-search；不用不写。
- runtime、model：不写 = 同你的模型；写就对上 models.yaml 的一个档位。
- priority：1–9，默认 5。

### 开发计划 plan:
- purpose：要解决什么问题。
- tools：新工具（最多 3 个），每项：
  - name：小写字母、数字、连字符；不能和已有工具、子代理重名。
  - functions：有哪些函数、各做什么。
  - network：要不要联网。
  - deps：Python 依赖，写死版本（如 requests==2.32.3）。
  - add_to：部署成功后加进哪些已有任务（任务名 → 工具项）；不写就不加。
  - docker：要起的一次性容器（同 tool.yaml 的 docker）；不起不写。
  - untrusted：输出里有别人写的自由文本（网页、邮件、评论…）就写 true：只给子代理用，不能 add_to 进任务。
  - hooks：工具的 hooks（同 tool.yaml）；不要不写。
- tasks：新任务（最多 2 个），每项：
  - name、description。
  - trigger：schedule / manual / listen；schedule：5 段 cron（本机时区），只给 schedule。
  - priority：1–9。
  - runtime、model：agent 任务写，一起对上 models.yaml 里能当主模型的档位（有 endpoint、context_window）；workflow 任务不写。
  - enabled_after_deploy：试跑成功后上不上线。
  - timeout：agent 任务是主模型时间上限，workflow 任务是各步骤在跑的时间合计；不写由开发时定。
  - tools：工具名 → 工具项（一般 {}；要追加本任务的用法规则写 guide）。
  - subagents：主模型能调用的子代理（已有的或本计划新建的）→ 子代理项。
  - memory：每次运行注入的记忆文件或文件夹（只读，文件夹 = 那一层所有 .md）；不要就写 []。
  - listen：监听名（只给 trigger: listen）；写 listen_py（listen.py 全文：check(ctx) 定时检查或 on_event(event, ctx) 订阅事件）= 定义新监听，不写 = 引用已有的同名监听；listen_events：订阅的事件来源，不订阅不写。
  - steps、network、resumable、deps：workflow 任务（不起 agent，按步骤在宿主上跑任务文件夹里的 py；不写 runtime、model、tools、subagents、memory）。steps 每步 [编号, py 文件名, 要不要资源]，要平台起模型服务的步骤加第 4 项 {runtime, model}；network 是步骤要不要联网；resumable 默认 true，步骤中间不能打断才写 false；deps 是写死版本的 Python 依赖。
  - hooks：任务的 hooks（同任务 yaml，脚本放任务文件夹），不要不写。没特别说明时，agent 任务（私信任务除外）默认把成品记进任务消息库：post_call 跑 record_run.py（match: [reply]），run_end 跑 record_run.py。
- change_tools：改已有工具（is_editable 为 true 的），每项 name、change（改什么、为什么）、fields（只写要改的声明：network、docker、is_editable、hooks）、deps（改依赖才写：requirements.txt 的完整新内容，[] = 不要依赖；不写 = requirements.txt 不能动）。
- change_tasks：改已有任务，每项 name、change、tools_add、tools_remove、subagents_add、subagents_remove、memory、fields、listen_py、deps（workflow 任务改依赖才写）。fields 只写 trigger、schedule、enabled、priority、listen、listen_events、steps、network、hooks、runtime、model（agent 任务换主模型）、timeout、resumable（只给 workflow 任务）；改 priority 要有充分理由，不要改优先级是 10 的任务。
- subagents：新子代理（文件夹 subagents/<名>/；不能是隔离的），每项 name、config（subagent.yaml 全文）、files（文件名 → 做什么）。
- change_subagents：改已有子代理（is_editable 不是 false 的），每项 name、change、fields、files。
- retire_tools：下线已有工具（is_editable 为 true 的），每项 name、reason、remove_from（还在用它的任务）。
- retire_tasks：注销已有任务，每项 name、reason。
- models：新工具要跑新模型时的新档位，每项 name、profile（runtime、model_path、endpoint、start、stop、min_avail_gb…）。

- 至少写一项新工具 / 新任务 / 新子代理，或改 / 下线一项已有的；用不到的键不写。
- is_editable: false 的工具、子代理不能改；平台代码不能改。要改它们得 Joy 先开放权限。
- 工具要的凭证不用列：代码里用到时读，缺了 builder 会让 Joy 填。
- 要下载的权重或文件、要给 ComfyUI 装的模型 / 节点不写进计划：builder 开发时用 download 工具申请下载、按规范装。
