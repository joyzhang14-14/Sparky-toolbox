## 要凭证的工具（credential-tool）
- 凭证不在计划和 tool.yaml 里列：代码用到时才读，`from control_center.credential import credential`，`credential("STB_X")` 返回值（键名用 STB_ 开头的大写字母、数字、下划线）。
  .env 里没有时它抛 MissingCredential（不要接住它）。只在真要用的那一刻读，不要在模块顶层读（工具会起不来）。
- 想知道 .env 里有哪些凭证，读 ~/sparky-toolbox/.env.keys；不要读 .env 本身。
- 值永远不进文件：仓库里出现 .env 的原值，凭证值扫描会拒（verify / 部署都查）。任务 yaml 也不能用 ${STB_X} 展开凭证。
- 代码里用了 credential("…") 的工具必须有 <工具名>_check()：无参数，用凭证做一次最小的只读调用（如登录后返回账号名），不回显凭证；失败 raise ToolError(原因)。
- 开发时 .env 还没有的键：用 builder_request_credential(key, note) 请 Joy 填；note 写清楚是什么、去哪拿。只能请求 .env 里还没有的键。builder_status 的 credentials 看填没填。
- 部署：合入前在真实运行环境里起工具、调一次 _check，没过不合入；合入后再查一次，没过的先不加进 add_to 的任务，Joy 重填后用 builder_check(工具) 验证，过了自动补加。
_check 写法需要时读 control_center/builder/examples/credential-tool-check.py。
常见错误：_check 回显了凭证；在模块顶层读凭证；接住 MissingCredential 改成别的错；测试里读真凭证（测试用 monkeypatch 设假的环境变量）。
