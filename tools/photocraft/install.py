"""部署脚本（合入之前跑一次，以 sparky 身份）：把已下载校验的 photocraft 官方 aarch64 包挪进手册规定的项目目录：
/home/joyzhang14/apps/photocraft/photocraft-0.10.0-linux-aarch64/ + 软链 /home/joyzhang14/apps/photocraft/photocraft-current
（和 ComfyUI、llama.cpp 放一起；包 sha256 829780d331e6443ddf1bada932ed07e0e9dc541b213b275f3c3a3209607bc7c7 已由 Joy 下载时校验）。

幂等：目标已装好就直接过；没装好且下载位置有包就挪过去；都没有 → 退出码非 0（不合入）。
挪动失败会自己挪回去再非 0 退出。"""
import shutil
import subprocess
import sys
from pathlib import Path

APPS = Path("/home/joyzhang14/apps/photocraft")
DEST = APPS / "photocraft-0.10.0-linux-aarch64"
LINK = APPS / "photocraft-current"
CLI = "bin/photocraft-cli"
SOURCES = [Path("/home/sparky/apps/photocraft-0.10.0-linux-aarch64"),
           Path("/home/sparky/apps/photocraft")]


def main() -> int:
    if (DEST / CLI).is_file():
        print(f"已装好：{DEST}")
    else:
        src = next((s for s in SOURCES if (s / CLI).is_file()), None)
        if src is None:
            print(f"找不到 photocraft 包：{DEST} 不存在，下载位置也没有（{'、'.join(str(s) for s in SOURCES)}）。"
                  "要 Joy 先把官方 aarch64 包（photocraft-0.10.0-linux-aarch64）放到 /home/sparky/apps/ 或手动装好", file=sys.stderr)
            return 1
        APPS.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(src), str(DEST))
        except Exception as e:
            if DEST.exists():                                   # 撤回去，别留半截
                shutil.move(str(DEST), str(src), copy_function=shutil.copytree)
            print(f"挪动 {src} → {DEST} 失败（已撤回）：{e}", file=sys.stderr)
            return 1
        print(f"已挪 {src} → {DEST}")
    if LINK.is_symlink() or LINK.exists():
        LINK.unlink()
    LINK.symlink_to(DEST.name)
    r = subprocess.run([str(LINK / CLI), "--version"], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        print(f"photocraft-cli 跑不起来：{r.stderr.strip()}", file=sys.stderr)
        return 1
    print(f"OK：{LINK} → {DEST.name}；{r.stdout.strip()}")
    old = Path("/home/sparky/apps/photocraft")                  # 挪走后 /home/sparky/apps 里可能剩个悬空软链，顺手清掉（只删悬空的）
    if old.is_symlink() and not old.exists():
        old.unlink()
    return 0


sys.exit(main())
