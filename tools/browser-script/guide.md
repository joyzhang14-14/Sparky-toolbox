# browser-script 用法（要掐时间的事：抢课、抢名额）

1. 让子代理 isolated-browser 把流程走一遍（停在最后提交前），prompt 里让它用 browser_record 存成脚本草稿（工作目录 scripts/<名>.mjs），并加一题「脚本草稿的路径」。
2. 用 read / edit 读草稿、改好（Playwright）：导出 start_url 和 run；条件触发的另写 check，返回 true 或 false。
   check 里不要用 try/catch 吞掉错误（登录过期、网站改版时 Joy 才收得到提醒）。草稿里的 TODO 都要处理；定位用 getByRole / getByLabel / getByText，不用元素编号。
3. 提交、发送、付款、选课确认一律写在 final(...) 里：演练时 final 里的不执行；final 外面不点提交。点完要 waitFor 等结果，否则会误判失败、重复提交。实跑时 final 最多试 40 次 / 30 秒，成功一次就停。
4. browser_script_run(path, mode="dry", profile=…) 演练：跑到 final 前停下。按报错行号改，直到结果是 stopped_at_final；
   条件脚本再用 mode="check" 试一次。profile 写登录档案名（同 isolated-browser）。
5. browser_script_schedule(name, script, at / watch, interval) 登记：定时写 at="YYYY-MM-DD HH:MM:SS"（本机时间，提前 10 分钟开跑，脚本等到整点才提交）；
   条件写 watch=true（每 interval 秒跑一次 check()，默认 30、最短 5）。脚本改过要重新演练。name 写短名字。
   不用另外问 Joy。
6. 触发时你会收到【紧急任务】消息：照它调 browser_script_result(name) 等结果，把结果和截图告诉 Joy；失败按消息说的用 isolated-browser 接手。

browser_script_run / browser_script_result 返回执行器记录：结果（done / stopped_at_final / final_failed / done_without_final / error / missed / script_changed / true / false）、
出错的行号和报错、跳转过的网址、写入请求、截图路径；不含页面正文。
