## 下线（retire）
什么时候用：Joy 说不要了的、被替代的、坏了也不打算修的工具 / 任务。只是暂时不跑用 change_tasks 的 fields.enabled: false。
能下线：tool.yaml 没写 is_editable: false 的工具（写了的告诉 Joy 需要先开放权限）；一次性任务以外的已有任务。定义了监听、还有别的任务引用它的，先把定义迁走或撤掉引用（见 listen）。
计划 retire_tools 每项：name、reason、remove_from；retire_tasks 每项：name、reason。需要时读 control_center/builder/examples/retire-plan.json。
- remove_from：tools 里还写着这个工具的每个已有任务都要列上，或把那个任务一起写进 retire_tasks。子代理的 tools 里用着的不能下线：先另提计划用 change_subagents 去掉。
- 本计划别处不能再用 / 改要下线的东西。
- 开发区里：git rm 要删的文件（工具是 tools/<名>/ 整个，任务是 tasks/<名>/ 整个，外加 tests/stb_platform/test_<名>.py），按 remove_from 改那些任务 yaml（只删这个工具那一项），再 commit；别的测试文件不用改（平台测试用的是 tests/stb_platform/fixtures/sample-repo/ 里的样本，不读真实的任务和工具）。
- 要注销的任务在跑或在排队时合入不了，等它跑完再 builder_submit。
常见错误：漏了还在用这个工具的任务；只删了 tool.yaml 没删文件夹里别的文件或测试；顺手改了别的文件。
