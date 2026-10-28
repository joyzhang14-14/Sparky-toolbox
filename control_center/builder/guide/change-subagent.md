## 改已有子代理（change-subagent）
计划 change_subagents 每项：name、change（改什么、为什么）、fields（要改的 yaml 字段 → 新值）、files（文件名 → 改什么）。需要时读 control_center/builder/examples/change-subagent-plan.json。
- 只能改 yaml 没写 is_editable: false 的子代理。
- fields：要改的 yaml 字段 → 新值（null = 删掉可选字段）；name、is_editable、isolated 不能改。改完的内容照样按子代理规则校验（字段见 new-subagent 与 stb schema subagents），
  换档位的 runtime + model 要对上 models.yaml 或本计划的新档位；agent 的 tools 不能加读外部内容的工具。
- files：要新建 / 改 / 删的文件夹里 yaml 以外的文件（文件名 → 改什么）；没列的文件部署时必须和原来一模一样，yaml 只改 fields 里的字段（别的原样不动）。
- 用它的任务要加 / 去掉它写在 change_tasks 的 subagents_add / subagents_remove（见 change-task）。
- 模型文档：换模型、加档位后用 ask_user 告诉 Joy 要在 Obsidian 的 Sparky/ModelOverallStatus.md 更新什么（档位名、模型、占用内存、用途），由他更新。
常见错误：fields 里写了 name / is_editable；改了 files 没列的文件；yaml 里顺手改了 fields 没写的字段。
