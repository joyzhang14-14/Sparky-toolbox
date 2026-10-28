"""控制台：网页上看和管任务的那部分（任务、运行记录、日志、工具、子代理；立即跑、启停、暂停、改优先级、加减工具）。
接口在 api.py（get / post 按路径分发），前端在 frontend/（console.js、console.css）；网页服务外壳 channels/web/server.py 挂上它们（渠道 import 平台，这里不 import 渠道）。
"""
