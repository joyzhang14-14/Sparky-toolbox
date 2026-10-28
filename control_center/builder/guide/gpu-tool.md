## GPU 工具（gpu-tool）
用平台 control_center/media.py 的 run_gpu(工具名, cmd, read, write, timeout=, need_gb=)：每次调用起一个一次性 stb-docker-base 容器（GPU、断网、只读根），跑完就退、释放显存；不起常驻服务、不换模型，和主模型同时跑。
- tool.yaml 写法需要时读 control_center/builder/examples/gpu-tool-tool.yaml。
- 镜像里只有系统和 python3.12：venv、模型、脚本放进 read（以宿主上的同一路径只读挂进去），命令用挂进去的 venv 的 python；成品目录 STB_MEDIA_DIR 放进 write。HF 缓存只挂 hub/。
- 镜像里没有 C 编译器：run_gpu 已设 TORCH_DISABLE_NATIVE_JIT=1；别的库要现场编译的（报 Failed to find C compiler）照同样思路关掉，用 builder_try_tool 在容器里真调验证。
- docker.memory 是容器内存（实测峰值 + 余量，写进注释），显存不算在里面。
- 开跑前用 media.avail_gb() 查可用内存，不够就报错写明「可用 X GB、要 Y GB」；显存不足时 run_gpu 会报错，不要自己重试。
- 成品写进 STB_MEDIA_DIR，文件名用 media.next_name(种类) 起，返回成品路径（media.sandbox_path）。
常见错误：模型或 venv 目录没挂进容器；在容器里联网下载模型；把 torch 写进计划 deps（用宿主已有的 venv）。
