# stb-docker-base：通用底座镜像，给要在容器里跑宿主 venv 的工具用（现在是 cutout、transcribe，见 control_center/media.py 的 run_gpu）。
# 镜像本身不带 GPU（GPU 由 tool.yaml 的 docker.gpu: true 在起容器时给），只提供 Ubuntu 24.04 + /usr/bin/python3.12，
# torch 等不进镜像：工具的 venv（~/apps/ComfyUI/venv、~/venvs/qwen-asr，python 软链到 /usr/bin/python3）、模型、helper 脚本由 stbcc-docker
# 以宿主上的同一路径只读挂进来，只写本次运行的 media 目录。运行时（GPU 工具）--gpus all、--network none、只读根、无能力、以调用方（sparky）身份跑。
# 基础镜像用本机已有的 playwright noble（自带 python 3.12.3，与宿主一致；层与 stb-render 共用，不另占磁盘），构建不联网。
# 构建：docker build -t stb-docker-base:latest services/stb-docker-base
FROM mcr.microsoft.com/playwright:v1.63.0-noble
ENV NVIDIA_DRIVER_CAPABILITIES=compute,utility HOME=/tmp PYTHONDONTWRITEBYTECODE=1
CMD ["/usr/bin/python3", "-V"]
