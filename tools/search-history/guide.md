# search-history 用法

search_history(query, source, since, until, limit, order)：
- query：关键词，空格分开的几个词都要出现（子串匹配，不分大小写）；字面命中的排前面，不够 limit 条时后面接上意思相近的。
- source：dm（默认）查 Joy 和私信助手的对话（Joy 说的、问题卡上的回答、你的回复）；task 查任务发给 Joy 的消息（日报、新闻这类成品与运行结果，带任务名）和 Joy 在这些任务问题卡上的回答。
- since / until：可选，时间段 [since, until)，写 YYYY-MM-DD 或 YYYY-MM-DD HH:MM（本机时区）。
- limit：最多返回几条（默认 20，最多 100）。
- order：relevance（默认，按相关度，同分新的在前）或 time（新的在前，找最近说过的用它）。
