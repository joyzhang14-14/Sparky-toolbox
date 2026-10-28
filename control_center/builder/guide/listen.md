## 监听（listen）
只给 trigger: listen 的任务。监听是有名字、可复用的定义：定义者任务的文件夹 tasks/<名>/ 里放 listen.py，由它决定听什么、什么时候触发、这次的用户消息是什么；触发就入队。
- 定义：计划写 listen（监听名）+ listen_py（listen.py 全文，文件要和它一字不差）、listen_events（订阅的事件来源）、network（listen.py 联不联网）。同名只能有一个定义。
- 引用：只写 listen: <已有的监听名>，不写 listen_py。触发时定义者和所有引用者用同一个 prompt 各自入队；监听按定义者的 network 跑。
- 定义者还有任务引用时，不能下线、不能改监听名：先给一个引用者写同样的 listen_py（定义迁过去），或先撤掉引用。
两个函数（普通函数，不是 async；写一个或两个）：
- check(ctx)：定时检查，每 30 秒调一次（不能改；要更稀就按 ctx["now"] 和 state_dir 里记的时刻自己跳过）。
- on_event(event, ctx)：listen_events 里的来源一有事件就调，同一来源按到达顺序逐条交；event 里有 source（来源名）和来源给的字段。来源：message（Joy 发来的消息，字段 text、channel、origin、msg_id、ts，带附件时有 images、docs）；一次性回调（来源是登记它的工具名，字段 name、prompt；只交给登记它的任务）。
返回一个 dict：fire（true / false）、prompt；fire 为 false 就不入队、不起模型；prompt 可省略，这次只靠 system_prompt 跑；要带以前的对话由 listen.py 自己拼进 prompt。on_event 还可以返回 reply（回给发布方的一句话）、steer（可以插进本任务正在跑的对话时用的文字）。
ctx（只读）：task、tasks（这次触发会入队的任务）、listen、why（interval / event）、now（时间戳）、state_dir（这个监听自己的可写目录：记已读位置等状态）、last_run（最近一次运行的 id、status、started、channel，没有为 null）、model（任务这次用的模型档位，含 context_window）。
运行：每次调用限时 60 秒；network 不写 = 不联网。任务在排队、在跑或在等 Joy 时不调 check，跑完再查，所以已读位置别提前推进。
写法需要时读 control_center/builder/examples/listen-news.py（每 10 分钟查一次网站，有新条目才跑）、control_center/builder/examples/listen-daily.py（每天 9 点后查一次，多数天没事就不跑）。
什么时候写监听：经常没事、或要先筛选再交给模型 → 写 check；每次都有事、模型自己去读就行 → 不写监听，在 system_prompt 里写「去哪看」。
外部内容：prompt 直接进模型，只放「有什么新东西」（标题、编号、数量），原文让任务交隔离子代理去读。
常见错误：函数写成 async 或参数个数不对；listen.py 和计划的 listen_py 不一致；把已读位置写在 state_dir 以外；想复用已有监听却又写了 listen_py。
