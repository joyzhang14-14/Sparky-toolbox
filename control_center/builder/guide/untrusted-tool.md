## 外部内容工具（untrusted-tool）
判断：输出里有别人写的自由文本（网页、邮件、评论、RSS、聊天记录、文件内容……）会进到模型 → untrusted: true（这些字可能藏着指令，不能直接进主模型）；只有自己算出来的数字、状态、平台生成的路径和固定格式字段 → 不标。拿不准就标。
- 计划 tools 里这个工具写 untrusted: true（需要时读 control_center/builder/examples/untrusted-tool-plan.json）；tool.yaml 写 usable_by: subagent，并用注释写明「读外部内容」。
- 这类工具只给隔离子代理（isolated: true）用：任务的 tools、add_to、builder 建 / 改的子代理的 tools 里都不能写。要让某个隔离子代理用它，在计划说明里写清，部署后由 Joy 加。
- 别为了绕开隔离把外部原文拆进「可信」字段、写进日志 / 状态文件让别的工具读、或塞进 listen.py 返回的 prompt。
- 已有工具的 usable_by 计划不能改。
常见错误：输出里夹着网页 / 邮件原文却没标 untrusted；tool.yaml 没写 usable_by: subagent；把这类工具写进任务的 tools / add_to。
