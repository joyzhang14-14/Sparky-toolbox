## 改已有工具（change-tool）
只能改 yaml 没写 is_editable: false 的工具（builder 做的工具也写在这里）。
计划 change_tools 每项：name、change（改什么、为什么）、fields（要改的声明 → 新值）、deps（改依赖才写）。
- 能改的文件是它的文件夹 tools/<名>/ 里的，测试写 tests/stb_platform/test_<名>.py。
- fields 能写：network、docker、is_editable、hooks；值 null = 删掉这一项。
- 没写进 fields 的这些声明部署时必须原样不变；description 直接改文件、在 change 里说明。
- usable_by 关系到外部隔离，计划不能改（要改用 ask_user 问 Joy）。
- 依赖：要改 tools/<名>/requirements.txt 就在计划写 deps = 它的完整新内容（写死版本，同 new-tool；[] = 删掉这个文件、改用平台的 venv），文件要和 deps 一模一样；开发区里装在 /deps/<名>。没写 deps 时 requirements.txt 不能动。
需要时读 control_center/builder/examples/change-tool-plan.json。
常见错误：改了 fields 没写的声明（部署报「计划没写要改它」）；改了同文件夹里别的文件（超出范围）；改函数参数的默认值前没想到别的任务会跟着变。
