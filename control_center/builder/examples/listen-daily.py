"""监听示范：每天 9 点后导入当天待办，一天最多一次，多数天没事就不跑。
task.yaml 里写：
    trigger: listen
    listen: daily-agenda
"""
import datetime
from pathlib import Path


def check(ctx):
    now = datetime.datetime.fromtimestamp(ctx["now"])
    done = Path(ctx["state_dir"]) / "done-day"
    if now.hour < 9 or (done.exists() and done.read_text() == str(now.date())):
        return {"fire": False}
    done.write_text(str(now.date()))                # 今天查过了：没事也不再查
    f = Path("/home/joyzhang14/Obsidian/Daily") / f"{now.date()}.md"   # 写全路径，不用 ~
    items = [line for line in (f.read_text().splitlines() if f.exists() else []) if line.startswith("- [ ]")]
    if not items:
        return {"fire": False}                      # 本次不跑、不起模型
    return {"fire": True, "prompt": "今天要办：\n" + "\n".join(items)}
