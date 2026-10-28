## ComfyUI 工具（comfyui-tool）
工具不能让平台换模型：要卸下主模型、换成 ComfyUI 档位才能跑的生成，写成 generator 子代理（计划的 subagents，见 new-subagent；同 image / video），程序按下面的接口干活，
平台在调用前后换模型；builder 的 ComfyUI 工具只在 ComfyUI 已经在跑时能用。
本机 ComfyUI：http://127.0.0.1:8188（models.yaml 的 ComfyUI 档位 endpoint）。builder 的工具只能写 network: true / false，连本机端口要 network: true。全走 HTTP 接口，不碰 ComfyUI 目录：
- GET /system_stats：在不在跑（先查）。
- POST /upload/image（multipart：image 文件、type=input、subfolder=stb/<运行 id>、overwrite=true）：传输入图，返回 {name, subfolder}，工作流里 LoadImage 用 "<subfolder>/<name>"。
- POST /prompt {"prompt": 工作流, "prompt_id": uuid, "client_id": "stb"} → 轮询 GET /history/<prompt_id>，直到 status.completed 或 status_str == "error"（错误在 status.messages 的 execution_error）。
- GET /view?filename=&subfolder=&type=output：下载成品，存进本次运行的 media 目录（工具是 STB_MEDIA_DIR；子代理的程序用平台 control_center/media.py 的 media_dir()），返回成品路径（media.sandbox_path），结果附预览图（media.preview）给主模型看。
- 超时或出错：POST /queue {"delete": [prompt_id]}、POST /interrupt {"prompt_id": …}，别留着占机器。
工作流用 ComfyUI 的「Save (API)」格式，存在 tools/<名>/workflows/*.json，代码里按节点 id 改参数。参考 subagents/image/_comfy.py（要用就复制进自己文件夹）。
没在跑（/system_stats 不通）就 raise ToolError 写明原因。generator 子代理的工作流怎么验证：单测过了用 builder_try_subagent 真调一次，ComfyUI 拒工作流这类错只有真跑才暴露；不要让 Joy 手动卸主模型来测。
ComfyUI 里的输入图与成品副本（ComfyUI 安装目录下的 input/stb/<运行 id>/、output/stb/<运行 id>/）：generator 子代理自己写 post_call hook 删本次运行的这两个目录。
运行 id 取 hook 输入的 run_id；输入的 inflight > 0（同一运行还有调用在途）就先不删，等最后一个；安装目录写全路径；删前解析符号链接，核对路径在这两个目录之下。
generator 换模型前的输入检查（pre_call hook）、给主模型的用法与重试规则（guide.md）见 new-subagent。要新模型 / 节点见 comfyui-assets。
常见错误：工作流是界面格式不是 API 格式；节点类名拼错（/object_info 里查）；生成超时没撤单。
