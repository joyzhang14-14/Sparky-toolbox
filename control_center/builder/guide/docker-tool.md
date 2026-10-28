## docker 工具（docker-tool）
要容器时用平台 control_center/docker_client.py 的 run() 起一次性容器（根只读，资源按 tool.yaml 的上限），不直接调 docker。
- tool.yaml 写 docker 段，计划 tools[].docker 写成一样；自带 tools/<名>/Dockerfile 的，部署时平台建成镜像 stb-<名>。写法需要时读 control_center/builder/examples/docker-tool-tool.yaml。
- 调用写法需要时读 control_center/builder/examples/docker-tool-run.py。必须带 tool=<工具名> 和 run=STB_RUN_REF。
- mounts 每项：host（宿主路径）、container（容器路径）、mode（ro / rw）；只能挂工具本来就能碰的目录（ro 要能读，rw 要能读写）；成品目录 STB_MEDIA_DIR 挂 rw。
- 到时限抛 subprocess.TimeoutExpired：报「用了 X 秒 / 给了 Y 秒」，让模型决定要不要加大 timeout 再试。退出码 137 多半是内存不够。
常见错误：没带 STB_RUN_REF；挂了没有权限的目录；镜像名不在 docker.images 里；在测试里真起容器（测试换掉 docker_client.run）。
