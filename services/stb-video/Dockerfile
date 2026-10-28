# stb-video：edit-video 工具的一次性剪辑容器（tools/edit-video/server.py）：ffmpeg 按剪辑单剪切、拼接、变速、缩放、配乐、烧字幕，
# 另出抽帧拼图给主模型看。基于 stb-render（字体一致：烧字幕用同一套字体）。运行时 --network none、只读根、以 sparky 身份跑，
# 按原路径只读挂任务工作目录、只写本次运行的 media 目录。构建（先建好 stb-render）：docker build -t stb-video:latest services/stb-video
FROM stb-render:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/* && mkdir -p /opt/video
COPY --chmod=644 video.py /opt/video/
ENTRYPOINT ["python3", "/opt/video/video.py"]
