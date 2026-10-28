## ComfyUI 模型 / 自定义节点（comfyui-assets）
模型和节点都不写进计划。
- 模型（权重文件）：下到 /home/sparky/workspace/_download/ 后，用 shell 挪进 ComfyUI 的 models/<目录>/（checkpoints、loras、vae、diffusion_models、text_encoders…，可再分子目录）。已有同名文件不覆盖：工作流直接用它，或换个文件名。
- 自定义节点：写进用它的子代理文件夹的 comfy_nodes.yaml，计划 subagents / change_subagents 的 files 里列上它和 install.py。格式需要时读 control_center/builder/examples/comfyui-assets-comfy_nodes.yaml。
- install.py 照 subagents/image/install.py 写（把 subagents/image/_comfy.py 的 install_nodes 那部分复制进自己文件夹再调）。部署时合入前平台跑它，退出码不是 0 就不合入，原始输出交回给你；builder_verify 不跑它。装不上就用 ask_user 告诉 Joy 要装什么（仓库、版本、类名）。
- 模型文档：加了模型后用 ask_user 告诉 Joy 要在 Obsidian 的 Sparky/ModelOverallStatus.md 记什么（模型、占用内存、用途）。
常见错误：ref 写分支名；classes 写成节点显示名；下载完没挪进 ComfyUI 的 models/ 就让工作流去找。
