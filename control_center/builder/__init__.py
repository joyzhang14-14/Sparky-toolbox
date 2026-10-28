"""builder：任务的主模型调用子代理 builder（subagents/builder/，is_editable: false）加新工具 / 新任务 / 新子代理、改或下线能改的，或把长活交给一次性任务
（规格见 Obsidian 的 Sparky/How-to-set-a-task-on-Sparky.md）。

- 提案：主模型调用 builder 时 prompt 写提案（YAML：plan: 开发计划，或 oneoff: 一次性任务；格式见 subagents/builder/guide.md）。
  子代理的 pre_call hook（subagents/builder/proposal_hook.py，以平台身份跑）调 review_proposal / accept：解析、校验提案 → 发通用审批卡（acquire_permission 的 permission 卡）
  并就地等 Joy → 批准：开发计划建好开发区（prepare）后放行，子代理开始开发；一次性任务建好 tasks/oneoff-<名>/、入队，拦下这次调用、把结果交回主模型。
  没等到：拦下，网关就地等这张卡、等不到就让出运行（waiting），Joy 处理后续跑，主模型用同样的提案再调用就直接过（卡按提案指纹 card_use）。
- 一次性任务：只用已有工具和子代理，跑完（complete / failed / timeout / stopped）runner 调 remove_oneoff 自动注销，运行记录留着。
- 开发：子代理用 builder-dev 工具（tools/builder-dev/，只给子代理用）经 stbcc-builder 操作开发区：Linux 用户 sparky 的 /home/sparky/dev/<id>/
  （repo 克隆、wheelhouse、deps）；宿主上不在里面跑命令，git / python 一律在断网的 stb-dev 容器里以 uid 1001 跑（dev_run / write_file / 取分支）。
- 子代理：计划的 subagents 新建文件夹写法的子代理（subagents/<名>/：agent / generator / engine，不能是隔离的），change_subagents 改 is_editable 不是 false 的；
  审批卡列 yaml 全文 / 旧值 → 新值、要换的模型档位、文件夹里的文件，部署检查核对文件夹和计划一致（_validate_subagents、_check_subagents）。任务的 subagents 也写在计划里。
- is_editable: false 的工具、子代理（Joy 维护的、隔离的）一律不改：计划写了就拒，说明是 is_editable: false、要 Joy 先开放权限（LOCKED_NOTE）。
- builder_verify：按部署检查的全部规则查当前开发区（范围、字段、加载、凭证值、测试、真实环境试跑），不合入，次数不限。
- builder_submit（同步做到合入）：范围检查 → 压成一个提交接在当前 main 上 → 容器里跑全部测试 → 真实环境试跑 → 跑文件夹里的 install.py（run_installs）
  → ff 合入 main；任何一步失败都不合入，报错（原始或带改法）直接交回子代理。合入之后的事交给 worker（post_merge，独立的 systemd 单元，不挡子代理）：
  写新档位、装运行环境（有依赖的工具 / 任务建自己的依赖环境 data/runtime/venvs/tools|tasks/<名>，平台的依赖一起装；状态目录 data/tool_state/<名>；代码就是仓库的 tools/<名>/）、凭证检查、加进任务、
  stb task add（停用）→ 试跑 → 按计划启用 → 重启服务 → 私信 Joy。
- 工具（不论谁做的）运行时经 sudo -n -u sparky 起，文件看 sparky 的文件权限（平台开好它的状态目录、本次运行的 media 与本工具的运行目录），network: false 用 Landlock 禁 TCP；环境里没有凭证（用到时自己读 .env）。
所有外部命令都经 _run（测试替换它，不碰 sudo / docker / systemd）。
"""
from __future__ import annotations

import asyncio
import io
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from control_center import cards, env, launch, subagents
from control_center.builder.plan import NAME_RE, TOPICS, Plan, PlanTask, PlanTool, topic_lines  # noqa: F401 — 计划 schema
from control_center.paths import HOME, HUB, REPO, RUNTIME, TOOL_STATE, TOOL_USER, sparky_user, tool_home

SPARKY = TOOL_USER
SPARKY_HOME = tool_home()             # 开发区 dev/<计划>/ 与预演目录 stage/<计划>/ 在这里（上线后的运行环境在平台仓库里，见 venv_dir）
VENV = Path(sys.prefix)                 # 仓库 venv（~/sparky-toolbox/.venv）：只读挂进 stb-dev 容器当开发与测试环境
DEV_IMAGE = "stb-dev:latest"
DEV_USER = sparky_user() or "1001:1001"   # 开发容器以工具用户的 uid:gid 跑（按名字现查）；没有这个用户（builder 的测试容器）用镜像约定的 1001
ACTIVE = ("approved", "preparing", "ready", "deploying", "merged")
ENDED = ("deployed", "failed", "superseded", "rejected")   # rejected：data/hub/builder/plans 里早先被拒的计划记录（每日清理按它删）   # 计划结束：中间产物（PLAN_SCRATCH、试跑目录、开发区、预演目录）在进入这些状态时删
SHALLOW_DEV = False  # 2026-10 builder 复现测试期间临时：开发区克隆只留 main 最新一个提交（翻不到被测原件的旧版本），测试结束改回 False
PLAN_SCRATCH = ("wheelhouse", "check", "checkwork", "branch.bundle", "main.bundle")   # data/hub/builder/<计划>/ 下的中间产物；plan-<计划>.md、deploy.log 等小记录留着
UV = Path.home() / ".local" / "bin" / "uv"   # 平台的包管理器：平台依赖的锁定版本从仓库的 uv.lock 导出
PLATFORM_REQS = "platform-requirements.txt"   # wheelhouse 里平台依赖的锁定版本：自己的依赖环境都和计划依赖一起装（pip 一起解析），环境里平台代码照样能 import
OUT_CHARS = 20_000
MAX_FILE_BYTES = 1_000_000
TEST_TIMEOUT_S = 1200
CHECK_TIMEOUT_S = 120                   # 部署后凭证检查（<工具名>_check）的单次上限
ALLOWED_VARS = {"STB_OWNER_DISCORD_ID"}  # agent 写的任务 yaml 里唯一能展开的 ${}：别的键会把 .env 里的凭证展开进提示词
COMMAND_KEYS = ("steps", "network", "hooks")   # 在宿主上跑的东西的声明（workflow 的步骤与任务的 hook 都以 sparky 跑）：卡上逐字列出、标 ⚠️
ONEOFF_PREFIX = "oneoff-"                # 一次性任务名的前缀（tasks/.gitignore 里 oneoff-*/）
ONEOFF_DONE = ("complete", "failed", "timeout", "stopped")   # 一次性任务运行到这些状态就注销；paused / waiting / partial 还会续跑，不注销
LOCKED_NOTE = "{what} 是 is_editable: false，我改不了，需要你先开放权限才能写"   # builder 碰到 is_editable: false 的工具 / 子代理时的说法（报错与 guide 里一致）


class BuilderError(ValueError):
    topic = ""                            # 对应规范的主题（guide_text）：提案被拒时 explain 把它的全文附给主模型


def _locked(what: str, guide: str = "") -> BuilderError:
    """碰到 is_editable: false 的工具 / 子代理：不改，原话告诉 Joy（LOCKED_NOTE）。"""
    return _err(LOCKED_NOTE.format(what=what), f"不改它，把这句原话告诉 Joy：「{LOCKED_NOTE.format(what=what)}」；他把 is_editable 改成 true 之后再提", guide)


def _err(what: str, fix: str, guide: str = "") -> BuilderError:
    """给模型的整理过的报错：错在哪 + 具体改法 + 对应规范（builder_guide(主题)）。"""
    e = BuilderError(f"{what}\n改法：{fix}" + (f"\n规范：builder_guide(\"{guide}\")" if guide else ""))
    e.topic = guide
    return e


def _line(text: str, key: str) -> str:
    """yaml 原文里顶层键 key 在第几行（给报错指位置）；<块>.<项> 找那个块里的那一行。没写返回「没写这一项」。"""
    head, _, sub = key.partition(".")
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if re.match(rf"{re.escape(head)}\s*:", l):
            if not sub:
                return f"第 {i + 1} 行"
            for j in range(i + 1, len(lines)):
                if lines[j].strip() and not lines[j][0].isspace():
                    break
                if re.match(rf"\s+{re.escape(sub)}\s*:", lines[j]):
                    return f"第 {j + 1} 行"
            return f"第 {i + 1} 行的 {head} 块里没写 {sub}"
    return "没写这一项"


# ---------- 规范（control_center/builder/guide/<主题>.md，随代码维护；builder 子代理用 builder_guide(主题) 查） ----------
GUIDE_DIR = Path(__file__).resolve().parent / "guide"


def guide_text(topic: str) -> str:
    """一个主题的规范段落（$STB_DATA 换成平台 data 目录）。"""
    if topic not in TOPICS:
        raise _err(f"没有规范 {topic!r}", f"主题写这些之一：{', '.join(TOPICS)}")
    return (GUIDE_DIR / f"{topic}.md").read_text().replace("$STB_DATA", str(HOME)).strip()


# ---------- 计划（schema 在 control_center/builder/plan.py；builder-dev 工具从那里取规范主题 TOPICS） ----------


def _dir() -> Path:
    return HUB / "builder"


def _read_json(p: Path, default: Any) -> Any:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


def _write_json(p: Path, data: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, p)


def status_text() -> str:
    p = active_plan()
    return f"当前计划 {p['id']}：{p['status']}" + (f"（{p['error']}）" if p.get("error") else "") if p else "没有进行中的计划"


def venv_dir(name: str, kind: str = "tools") -> Path:
    """有 requirements.txt 的工具 / 任务上线后的依赖环境：data/runtime/venvs/<tools|tasks>/<名>（平台用户拥有，sparkle 组只读；registry.venv_dir）。"""
    from control_center.registry import venv_dir as vd

    return vd(name, kind)


def platform_requirements() -> str:
    """平台 venv 的锁定版本（仓库 uv.lock 导出，不含开发依赖与平台自身）：pip 的 requirements 格式（带平台标记）。"""
    return _ok(_run([str(UV), "export", "--frozen", "--no-hashes", "--no-dev", "--no-emit-project", "--no-header", "--no-annotate"], cwd=REPO, timeout=120),
               "导出平台依赖的锁定版本").decode(errors="replace")


def dep_owners(plan: dict[str, Any]) -> list[tuple[str, str, list[str]]]:
    """计划里要（重）建依赖环境的 (tools | tasks, 名, deps)：写了 deps 的新工具 / 新任务，写了非空 deps 的改已有工具 / 任务（plan 是计划的 dict）。"""
    return ([("tools", t["name"], t["deps"]) for t in [*plan["tools"], *(plan.get("change_tools") or [])] if t.get("deps")]
            + [("tasks", k["name"], k["deps"]) for k in [*plan["tasks"], *(plan.get("change_tasks") or [])] if k.get("deps")])


def _deps_target(kind: str, name: str) -> str:
    """开发区容器里装计划依赖的目录：工具 /deps/<名>，任务 /deps/tasks/<名>（在 PYTHONPATH 里）。"""
    return f"/deps/{name}" if kind == "tools" else f"/deps/tasks/{name}"


def _pkg(req: str) -> str:
    return re.sub(r"[-_.]+", "-", req.split(";")[0].split("==")[0].strip()).lower()


def platform_conflicts(deps: list[str]) -> list[str]:
    """计划依赖里和平台锁定版本不同的包（自己的依赖环境要和平台的依赖一起装，版本不能不同）：「包==计划版本（平台用 包==平台版本）」。"""
    plat = {_pkg(x): x.split(";")[0].strip() for x in platform_requirements().splitlines() if "==" in x}
    return [f"{d}（平台用 {plat[_pkg(d)]}）" for d in deps if _pkg(d) in plat and plat[_pkg(d)].split("==", 1)[1] != d.split("==", 1)[1]]


# ---------- 计划状态 ----------
def _plan_path(pid: str) -> Path:
    if not re.fullmatch(r"[a-z0-9]{4,20}", pid):
        raise BuilderError("非法的计划 id")
    return _dir() / "plans" / f"{pid}.json"


def load_plan(pid: str) -> dict[str, Any]:
    p = _read_json(_plan_path(pid), None)
    if p is None:
        raise BuilderError(f"没有计划 {pid}")
    return p


def save_plan(p: dict[str, Any], status: str | None = None, error: str | None = None) -> None:
    if status:
        p["status"] = status
        p.setdefault("history", []).append({"status": status, "at": time.time(), **({"error": error} if error else {})})
    if error is not None:
        p["error"] = error
    elif status:
        p.pop("error", None)              # 状态往前走了：上一次的失败原因不再挂着
    _write_json(_plan_path(p["id"]), p)


def plan_leftovers(pid: str) -> tuple[list[Path], list[Path]]:
    """计划 pid 现在还在的中间产物：(平台侧：data/hub/builder/<计划>/ 下的 PLAN_SCRATCH、data/hub/builder-trial/<计划>/；
    sparky 侧：/home/sparky/dev/<计划>/、/home/sparky/stage/<计划>/)。"""
    if not re.fullmatch(r"[a-z0-9]{4,20}", pid):
        raise BuilderError("非法的计划 id")
    plat = [_dir() / pid / x for x in PLAN_SCRATCH] + [HUB / "builder-trial" / pid]
    return [x for x in plat if x.exists() or x.is_symlink()], [x for x in (dev_dir(pid), _stage_home(pid)) if x.exists()]


def clean_plan(pid: str) -> list[str]:
    """删计划 pid 的中间产物（计划结束时、每日清理兜底时调）。平台侧的解析后必须正好是 data/hub/builder/<计划>/<PLAN_SCRATCH 之一> 或 data/hub/builder-trial/<计划>，
    以平台用户删；sparky 侧的解析后必须正好是 /home/sparky/{dev,stage}/<计划>，以 sparky 执行 rm -rf。不合规则、删不了的写进返回的说明，不抛错。"""
    plat, sp = plan_leftovers(pid)
    bad = []
    for x in plat:
        real = Path(os.path.realpath(x))
        if real not in {Path(os.path.realpath(_dir() / pid)) / n for n in PLAN_SCRATCH} | {Path(os.path.realpath(HUB / "builder-trial")) / pid}:
            bad.append(f"{x}（解析后不在 data/hub/builder/{pid}/ 或 data/hub/builder-trial/{pid}，没删）")
            continue
        try:
            shutil.rmtree(real) if real.is_dir() else real.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            bad.append(f"{x}（{type(e).__name__}: {e}）")
    for x in sp:
        if os.path.realpath(x) != os.path.join(os.path.realpath(x.parent), pid) or x.parent not in (SPARKY_HOME / "dev", SPARKY_HOME / "stage"):
            bad.append(f"{x}（解析后不是 /home/sparky/{{dev,stage}}/{pid}，没删）")
            continue
        try:
            sudo("rm", "-rf", str(x))
        except BuilderError as e:
            bad.append(f"{x}（{str(e)[:200]}）")
    return bad


def end_plan(p: dict[str, Any], status: str, error: str | None = None) -> list[str]:
    """计划结束（ENDED）：存状态，再删中间产物（一次性任务没有开发区，不用删）；返回没删掉的说明，也记进 deploy.log。"""
    save_plan(p, status, error)
    if p.get("kind") == "oneoff":
        return []
    bad = clean_plan(p["id"])
    _log(p["id"], f"计划 {status}，中间产物已删" + (f"；没删掉的：{'；'.join(bad)}" if bad else ""))
    return bad


def active_plan() -> dict[str, Any] | None:
    """未结束的那个开发计划（审批卡不过期：pending 的计划一直等 Joy，直到被新计划取代）。一次性任务的计划不算。"""
    for f in sorted((_dir() / "plans").glob("*.json")):
        p = _read_json(f, {})
        if p.get("status") not in ACTIVE or p.get("kind") == "oneoff":
            continue
        return p
    return None


def validate_plan(plan: Plan) -> None:
    from control_center.models import MAIN_FIELDS, main_missing, profile_for, profiles
    from control_center.paths import task_file
    from control_center.registry import TOOLS
    from control_center.schedule import parse as parse_cron

    if not (plan.tools or plan.tasks or plan.change_tools or plan.change_tasks or plan.subagents or plan.change_subagents or plan.retire_tools or plan.retire_tasks):
        raise _err("计划里至少要有一个新工具、新任务、新子代理，或改 / 下线一个已有的工具 / 任务 / 子代理",
                   "至少写一个新工具（tools）、新任务（tasks）、新子代理（subagents），或改已有的（change_tools / change_tasks / change_subagents），"
                   "或下线（retire_tools / retire_tasks）")
    tool_names = [t.name for t in plan.tools]
    task_names = [t.name for t in plan.tasks]
    if len(set(tool_names)) != len(tool_names) or len(set(task_names)) != len(task_names):
        raise _err("计划里的工具名 / 任务名有重复", "每个新工具 / 新任务的 name 各不相同")
    new = [*tool_names, *(s.name for s in plan.subagents)]
    if len(set(new)) != len(new):
        raise _err("计划里新工具与新子代理的名字有重复（工具和子代理也不能同名）", "每个新工具 / 新子代理的 name 各不相同", "new-tool")
    for n in new:
        if taken := _name_taken(n):
            raise _err(f"名字 {n} 已被{taken}占用，新工具 / 新子代理不能用", "换个没用过的名字；要改已有的写在 change_tools / change_subagents", "new-tool")
    owners = dep_owners(plan.model_dump())
    for kind, name, deps in owners:
        for d in deps:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9.+!-]*", d):
                raise _err(f"{'工具' if kind == 'tools' else '任务'} {name} 的依赖 {d} 没写死版本", "写成 包名==确切版本，如 requests==2.32.3",
                           "new-tool" if kind == "tools" else "workflow-task")
    if (deps := [d for *_, ds in owners for d in ds]) and (bad := platform_conflicts(deps)):
        raise _err(f"依赖和平台的版本冲突：{'；'.join(bad)}（自己的依赖环境要和平台的依赖一起装）", "改成平台用的版本，或去掉这项（平台已经装了的包不用写）", "new-tool")
    for t in plan.tools:
        _plan_decls(t)
        _plan_untrusted(t)
        if t.hooks is not None:
            _plan_hooks(f"工具 {t.name}", t.hooks, "new-tool")
        for k in t.add_to:
            if k in task_names:
                raise _err(f"工具 {t.name} 的 add_to 写了本计划的新任务 {k}", f"从 add_to 去掉 {k}，在新任务 {k} 的 tools 里直接写 {t.name}", "new-tool")
            if not task_file(k).exists():
                raise _err(f"工具 {t.name} 的 add_to 写的任务 {k} 不存在", "add_to 只写已有任务的名字（tasks/ 下的文件夹名）", "new-tool")
    _validate_changes(plan)
    _validate_retire(plan)
    _validate_listen_world(plan)
    validate_tiers(plan.model_dump())
    _validate_subagents(plan)
    models = plan_models(plan.model_dump())
    for k in plan.tasks:
        if task_file(k.name).exists():
            raise _err(f"任务 {k.name} 已存在", "要改它写在 change_tasks（改什么、工具增减、fields）；要做新任务换个名字", "change-task")
        if (k.trigger == "schedule") != (k.schedule is not None):
            raise _err(f"任务 {k.name}：trigger 是 schedule 时必须写 schedule，别的 trigger 不写（现在 trigger 是 {k.trigger}，schedule 是 {k.schedule!r}）",
                       "trigger: schedule 配 5 段 cron 的 schedule；manual / listen 去掉 schedule", "new-task")
        if k.schedule is not None:
            try:
                parse_cron(k.schedule)
            except ValueError as e:
                raise _err(f"任务 {k.name} 的 schedule {k.schedule!r} 不合法：{e}", "写 5 段 cron（分 时 日 月 周，本机时区），如 \"0 7 * * *\"", "new-task") from None
        if k.listen is not None:
            _plan_listen(k, plan)
        if k.timeout is not None:
            _plan_timeout(f"任务 {k.name}", k.timeout, "new-task")
        if k.hooks is not None:
            _plan_hooks(f"任务 {k.name}", k.hooks, "new-task")
        if k.steps is not None:
            _plan_steps(f"任务 {k.name}", k.steps, models, "workflow-task")
        else:
            prof = profile_for(models, k.runtime, k.model)  # pyright: ignore[reportArgumentType]
            if prof is None or main_missing(models[prof]):
                chat = [f"{p.get('runtime')} + {p.get('model_path')}" for n, p in profiles(models).items() if not main_missing(p)]
                raise _err(f"任务 {k.name}：runtime {k.runtime} + model {k.model} 不是 models.yaml 里能当主模型的档位（要有 endpoint、{'、'.join(MAIN_FIELDS)}）",
                           f"runtime + model 写成已有能当主模型的档位之一：{'；'.join(chat) or '（没有）'}；要新模型在 models 里写新档位", "new-task")
        for name, opts in k.tools.items():        # 工具都能给（审批卡逐个写明做什么与风险，Joy 批）
            if name not in tool_names and name not in TOOLS:
                raise _err(f"任务 {k.name} 用到的工具 {name} 既不是已有工具，也不在本计划里", "写已有工具名（stb tools list 能看到），或把它作为新工具写进 tools", "new-task")
            _subagent_only(f"任务 {k.name}", name, plan)
        for name in k.subagents:
            _subagent_exists(f"任务 {k.name}", name, plan, "new-task")


def _name_taken(name: str) -> str | None:
    """新工具 / 新子代理的名字被谁占了：已有工具（登记的或 tools/<名>/ 在）、已有子代理（subagents/<名>/ 在）；没被占返回 None。"""
    from control_center.registry import TOOLS

    if name in TOOLS or _occupied(REPO / "tools" / name):
        return f"已有工具 tools/{name}/"
    if _occupied(subagents.DIR / name):
        return f"已有子代理 subagents/{name}/"
    return None


def _occupied(d: Path) -> bool:
    """d 有实际内容：是文件 / 链接，或目录里有 Python 缓存（__pycache__、.pyc）以外的文件。删掉文件夹后只剩缓存的残留目录不算占用。"""
    if d.is_symlink() or (d.exists() and not d.is_dir()):
        return True
    return d.is_dir() and any("__pycache__" not in f.relative_to(d).parts and f.suffix != ".pyc" for f in d.rglob("*") if not f.is_dir() or f.is_symlink())


def _subagent_exists(where: str, name: str, plan: Plan, item: str) -> None:
    """任务的 subagents 里写的子代理：已有的（subagents/<名>/），或本计划 subagents 新建的。"""
    if name not in {s.name for s in plan.subagents} and not subagents.is_folder(name):
        raise _err(f"{where} 的 subagents 里的 {name} 既不是已有子代理，也不是本计划新建的",
                   f"写已有子代理名（stb subagents list：{', '.join(subagents.folder_names()) or '无'}），或把它作为新子代理写进计划的 subagents", item)


def _subagent_only(where: str, name: str, plan: Plan) -> None:
    """只给子代理用的工具（已有的 usable_by: subagent，或本计划新做、标了 untrusted 的）不能写进任务的 tools。"""
    from control_center.registry import TOOLS

    new = next((t for t in plan.tools if t.name == name), None)
    if (new.untrusted if new is not None else name in TOOLS and TOOLS[name].usable_by == "subagent"):
        raise _err(f"{where}：工具 {name} 读外部内容、只给子代理用，任务的 tools 里不能写",
                   f"从 tools 去掉 {name}；任务要读外部内容就调用读它的隔离子代理（任务 yaml 的 subagents 写它的名字；stb subagents list 里标了隔离的）", "untrusted-tool")


def _listen_world(plan: Plan) -> tuple[dict[str, list[tuple[str, str]]], dict[str, list[str]], dict[str, str]]:
    """计划部署之后的监听：(监听名 → 定义 [(任务, listen.py 源码)]，监听名 → 写了它的任务，部署之前的定义者 监听名 → 任务)。
    已有任务按 change_tasks 的 fields.listen / listen_py 改过、去掉 retire_tasks，再加本计划的新任务（写 listen_py 的是定义）。"""
    from control_center.task import LISTEN_FILE, _raw_task_paths

    gone, new = {r.name for r in plan.retire_tasks}, {k.name for k in plan.tasks}
    changes = {c.name: c for c in plan.change_tasks}
    defs: dict[str, list[tuple[str, str]]] = {}
    users: dict[str, list[str]] = {}
    before: dict[str, str] = {}
    for n, raw, path in _raw_task_paths():
        own = path.parent / LISTEN_FILE
        src = own.read_text() if own.is_file() else None
        if src is not None and isinstance(raw.get("listen"), str):
            before.setdefault(raw["listen"], n)
        if n in gone or n in new:
            continue
        c = changes.get(n)
        name = c.fields["listen"] if c is not None and "listen" in c.fields else raw.get("listen")
        if c is not None and c.listen_py is not None:
            src = c.listen_py
        if isinstance(name, str) and name:
            users.setdefault(name, []).append(n)
            if src is not None:
                defs.setdefault(name, []).append((n, src))
    for k in plan.tasks:
        if k.listen:
            users.setdefault(k.listen, []).append(k.name)
            if k.listen_py is not None:
                defs.setdefault(k.listen, []).append((k.name, k.listen_py))
    return defs, users, before


def _validate_listen_world(plan: Plan) -> None:
    """本计划碰到的监听名（新任务的、改动前后的、下线任务的）：同名只能有一个定义；有任务引用就要有定义——定义者被下线 / 改了监听名而还有引用者时拒，
    提示先把定义迁走或撤掉引用；引用了不存在的监听名时列出现有的。"""
    defs, users, before = _listen_world(plan)
    touched = {k.listen for k in plan.tasks if k.listen}
    for c in plan.change_tasks:
        touched |= {x for x in (_task_raw(c.name).get("listen"), c.fields.get("listen")) if isinstance(x, str)}
    for r in plan.retire_tasks:
        if isinstance(x := _task_raw(r.name).get("listen"), str):
            touched.add(x)
    for name in sorted(touched):
        d = defs.get(name) or []
        if len(d) > 1:
            raise _err(f"监听 {name} 有多个定义（{'、'.join(n for n, _ in d)} 都有 listen.py）：同名只能有一个定义",
                       f"要复用就只留一个定义，其余任务只写 listen: {name}（引用，不写 listen_py）；要另写一个就换监听名", "listen")
        if (refs := users.get(name) or []) and not d:
            if (owner := before.get(name)) is not None:
                raise _err(f"任务 {owner} 定义了监听 {name}（tasks/{owner}/listen.py），本计划下线它或改了它的监听名，但任务 {'、'.join(refs)} 还引用 {name}",
                           f"先把定义迁到另一个任务的文件夹：在 change_tasks 里给其中一个引用者写同样内容的 listen_py（它成为定义者）；"
                           f"或先撤掉这些任务的引用（change_tasks 的 fields 改 listen）", "listen")
            raise _err(f"任务 {'、'.join(refs)} 引用的监听 {name} 不存在：没有任务定义它",
                       f"引用已有的监听（现有：{'、'.join(sorted(defs)) or '没有'}），或给这个任务写 listen_py 定义它", "listen")


def _plan_listen(k: PlanTask, plan: Plan) -> None:
    """写了 listen 的新任务：监听名格式；写 listen_py 是定义、不写是引用（定义与引用的对应在 _validate_listen_world 里查）；
    监听程序（自己的 listen_py 或定义者的 listen.py）语法对、要被调的函数在且签名对；
    事件来源名格式。"""
    from control_center.task import NAME_RE, TaskError, listen_source_functions

    name = str(k.listen)
    if not NAME_RE.fullmatch(name):
        raise _err(f"任务 {k.name} 的监听名 {name!r} 格式不对", "写小写字母、数字、- _（如 news）", "listen")
    if k.listen_py is None:
        owner, src = (_listen_world(plan)[0].get(name) or [("?", "")])[0]
        label = f"监听 {name} 的 listen.py（定义者 {owner}）"
    else:
        src, label = k.listen_py, f"任务 {k.name} 的 listen_py"
    try:
        fns = listen_source_functions(src, label)
    except TaskError as e:
        raise _err(str(e), "改 listen_py 的语法错", "listen") from None
    if bad := [f for f, ok in fns.items() if not ok]:
        raise _err(f"{label} 里 {'、'.join(bad)} 签名不对", "写成 def check(ctx) / def on_event(event, ctx)（普通函数，不是 async）", "listen")
    if k.listen_py is not None and "watch" in fns:
        raise _err(f"{label} 里有 watch(ctx, emit)", "去掉 watch，改写 check 或 on_event：builder 写的监听只用 check(ctx)（定时检查）或 on_event(event, ctx)（事件推送）", "listen")
    if k.listen_events and "on_event" not in fns:
        raise _err(f"任务 {k.name} 订阅了 {k.listen_events}，{label} 里没有 on_event(event, ctx)", "加 def on_event(event, ctx)，返回 {\"fire\": …, \"prompt\": …}", "listen")
    if not k.listen_events and "check" not in fns:
        raise _err(f"{label} 里没有 check(ctx)", "加 def check(ctx)，返回 {\"fire\": …, \"prompt\": …}；或订阅事件（listen_events + on_event）", "listen")
    if bad := [e for e in k.listen_events if not NAME_RE.fullmatch(e)]:
        raise _err(f"任务 {k.name} 的 listen_events {bad} 格式不对", "写来源名：小写字母、数字、- _（如 message）", "listen")


def _trigger_text(k: PlanTask) -> str:
    """审批卡上的触发方式：监听写明监听名、检查间隔、订阅的事件。"""
    base = f"{k.trigger}{' ' + k.schedule if k.schedule else ''}"
    if k.listen is None:
        return base
    from control_center.task import LISTEN_EVERY_DEFAULT

    how = [f"订阅事件 {'、'.join(k.listen_events)}（on_event）"] if k.listen_events else [f"每 {LISTEN_EVERY_DEFAULT}s 调 check"]
    return f"监听 {k.listen}：{'，'.join(how)}"


def _listen_role_lines(task: str, name: Any, defines: bool, plan: Plan) -> list[str]:
    """审批卡：这个任务对监听 name 是「定义」还是「引用」，以及 check 触发时一起被触发的任务。"""
    if not isinstance(name, str) or not name:
        return []
    defs, users, _ = _listen_world(plan)
    if defines:
        others = [n for n in users.get(name) or [] if n != task]
        return [f"  监听 {name}：定义（tasks/{task}/listen.py）" + (f"；引用它的 {'、'.join(others)} 在 check 触发时一起触发" if others else "")]
    owner = (defs.get(name) or [("?", "")])[0][0]
    together = [owner, *(n for n in users.get(name) or [] if n not in (owner, task))]
    return [f"  监听 {name}：引用（定义在 tasks/{owner}/listen.py，本任务不放 listen.py）；check 触发时和 {'、'.join(together)} 一起触发"
            "（各自已在排队 / 在跑 / 等卡的跳过）"]


def _listen_lines(task: str, code: str | None, events: list[str], net: Any, changed: bool = False) -> list[str]:
    """审批卡：listen.py 全文（以 sparky 跑，联网）。"""
    if code is None:
        return []
    return [f"  ⚠️ {'改' if changed else ''}监听程序 tasks/{task}/listen.py（stbcc-hub 以 sparky 运行，能读写 sparky 能碰的文件；它返回的 prompt 直接作用户消息进模型）"
            f"：联网 {'是' if net is True else net if net else '否'}\n```python\n{code.rstrip()}\n```"]


TIER_REQUIRED = ("start", "stop", "min_avail_gb")   # 计划里的新档位必须写：启动 / 停止命令、占用内存


def validate_tiers(plan: dict[str, Any]) -> None:
    """计划里的新模型档位：不和已有档位重名，必须写 start / stop / min_avail_gb，和已有档位合起来按 models.yaml 的规则校验。"""
    from control_center.models import PROFILE_FIELDS, check_models, load_models

    have = load_models()
    names = [m["name"] for m in plan.get("models") or []]
    if len(set(names)) != len(names):
        raise _err("计划 models 里的档位名有重复", "每个新档位的 name 各不相同", "model-tier")
    for m in plan.get("models") or []:
        prof = m["profile"]
        if m["name"] in have:
            raise _err(f"档位 {m['name']} 已在 models.yaml 里", "新档位换个名字；改已有档位要 Joy 手动改（用 ask_user 问他）", "model-tier")
        if bad := sorted(set(prof) - set(PROFILE_FIELDS)):
            raise _err(f"档位 {m['name']} 的 {bad} 不认识", f"profile 只写这些字段：{', '.join(PROFILE_FIELDS)}", "model-tier")
        if missing := [k for k in TIER_REQUIRED if prof.get(k) in (None, "")]:
            raise _err(f"档位 {m['name']} 缺 {'、'.join(missing)}", "profile 里写 start / stop（启动 / 停止命令）和 min_avail_gb（占用内存 + 余量，GiB），审批卡上给 Joy 看", "model-tier")
        if isinstance(prof["min_avail_gb"], bool) or not isinstance(prof["min_avail_gb"], (int, float)) or prof["min_avail_gb"] <= 0:
            raise _err(f"档位 {m['name']} 的 min_avail_gb 是 {prof['min_avail_gb']!r}", "写正数（GiB），= 实测占用 + 余量", "model-tier")
    try:
        check_models({**have, **{m["name"]: m["profile"] for m in plan.get("models") or []}})
    except ValueError as e:
        raise _err(f"新档位不合格：{e}", "按上面说的字段改 profile（规则同 models.yaml：stb schema models）", "model-tier") from None


def plan_models(plan: dict[str, Any]) -> dict[str, Any]:
    """models.yaml 加上计划里的新档位（还没写进去时校验任务用）。"""
    from control_center.models import load_models

    return {**load_models(), **{m["name"]: m["profile"] for m in plan.get("models") or []}}


def write_tiers(pid: str, plan: dict[str, Any]) -> list[str]:
    """部署（合入之后）：把计划里的新档位接在 models.yaml 末尾（原文、注释不动），先备份到 data/hub/builder/<计划>/models.yaml.before；
    已有同名且内容一样的跳过，同名不一样的拒。返回写进去的档位名。"""
    from control_center.models import check_models
    from control_center.paths import MODELS_FILE

    tiers = {m["name"]: m["profile"] for m in plan.get("models") or []}
    if not tiers:
        return []
    text = MODELS_FILE.read_text()
    have = yaml.safe_load(text) or {}
    if clash := [n for n in tiers if n in have and have[n] != tiers[n]]:
        raise BuilderError(f"models.yaml 里已有档位 {clash}，内容和计划不同，没写（要 Joy 手动处理）")
    new = {n: v for n, v in tiers.items() if n not in have}
    if not new:
        return []
    block = f"\n# builder 计划 {pid}（{time.strftime('%Y-%m-%d')}，Joy 批准）加的档位\n" + yaml.safe_dump(new, allow_unicode=True, sort_keys=False)
    out = text.rstrip("\n") + "\n" + block
    try:
        check_models(yaml.safe_load(out))
    except ValueError as e:
        raise BuilderError(f"写入新档位后 models.yaml 校验不过，没写：{e}") from None
    backup = _dir() / pid / "models.yaml.before"
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    backup.write_text(text)
    tmp = MODELS_FILE.with_name(f".{MODELS_FILE.name}.{os.getpid()}.tmp")
    tmp.write_text(out)
    os.chmod(tmp, MODELS_FILE.stat().st_mode & 0o777)
    os.replace(tmp, MODELS_FILE)
    subprocess.run(["git", "-C", str(MODELS_FILE.parent), "-c", "user.name=stb-builder", "-c", "user.email=builder@sparky", "commit", "-q",   # models.yaml 进 git：
                    "-m", f"feat(models): 计划 {pid} 加档位 {'、'.join(new)}", "--", MODELS_FILE.name], capture_output=True)            # 立刻提交，main 不留未提交改动
    return list(new)


# ---------- 安装脚本（约定文件 install.py：工具 / 子代理文件夹里有就在部署时合入之前跑） ----------
INSTALL_FILE = "install.py"
INSTALL_TIMEOUT_S = 3600


def plan_folders(plan: dict[str, Any]) -> list[tuple[str, str]]:
    """计划新建或改动的工具 / 子代理文件夹：(tools|subagents, 名字)。"""
    return ([("tools", t["name"]) for t in plan.get("tools") or []] + [("tools", c["name"]) for c in plan.get("change_tools") or []]
            + [("subagents", x["name"]) for x in [*(plan.get("subagents") or []), *(plan.get("change_subagents") or [])]])


def _install_env(kind: str, raw: dict[str, Any], plan: dict[str, Any]) -> dict[str, str]:
    """安装脚本的环境：基本变量 + 平台的（同 hook）；子代理用自己的模型档位时带 STB_MODEL（同运行时，如 ComfyUI 的地址、安装目录、启停命令）。"""
    from control_center.gateway import profile_env
    from control_center.models import profile_for
    from control_center.registry import PLATFORM_ENV, TEST_ENV_PREFIX

    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(Path.home()), "LANG": "C.UTF-8", "TMPDIR": "/tmp", "PYTHONPATH": str(REPO),
           **{k: v for k, v in os.environ.items() if k in PLATFORM_ENV or k.startswith(TEST_ENV_PREFIX)}}
    if kind == "subagents" and raw.get("runtime") not in (None, subagents.INHERIT):
        models = plan_models(plan)
        if name := profile_for(models, str(raw.get("runtime")), str(raw.get("model"))):
            env.update(profile_env(models[name], name))
    return env


def run_installs(pid: str, p: dict[str, Any], check: Path) -> list[str]:
    """部署（合入之前）：计划新建 / 改动的文件夹里有 install.py 的，从 check 仓库跑一次（工作目录是它的文件夹）。
    身份看它的 is_editable：false 以平台身份跑；其余以 sparky 跑（文件夹先拷进预演目录 /home/sparky/stage/<计划>/install/，联网不限）。
    退出码不是 0 就不合入（脚本自己撤回已装的），原始输出交回。返回给 Joy 的说明行。"""
    from control_center import launch

    lines = []
    for kind, name in plan_folders(p["plan"]):
        folder = check / kind / name
        if not (folder / INSTALL_FILE).is_file():
            continue
        try:
            raw = yaml.safe_load((folder / ("tool.yaml" if kind == "tools" else subagents.YAML_FILE)).read_text()) or {}
        except (OSError, yaml.YAMLError):
            raw = {}
        env = _install_env(kind, raw, p["plan"])
        if raw.get("is_editable") is False:
            r = _run([sys.executable, INSTALL_FILE], timeout=INSTALL_TIMEOUT_S, cwd=folder, env=env)
        else:
            home = _stage_home(pid) / "install"
            sudo("rm", "-rf", str(home))
            sudo("mkdir", "-p", str(home))
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tar:
                tar.add(folder, arcname=f"{kind}/{name}")
            sudo("tar", "-x", "-C", str(home), "-f", "-", input=buf.getvalue())
            cmd = f"cd {shlex.quote(str(home / kind / name))} && {shlex.quote(sys.executable)} {INSTALL_FILE}"
            r = _run(launch.sparky_command(sys.executable, {**launch.net_cfg(True), "exec": cmd}, env, f"install:{kind}:{name}"), timeout=INSTALL_TIMEOUT_S, env=env)
        out = (r.stdout + r.stderr).decode(errors="replace").strip()
        _log(pid, f"{kind}/{name}/{INSTALL_FILE}：退出码 {r.returncode}：{out[-500:]}")
        if r.returncode != 0:
            raise _err(f"{kind}/{name}/{INSTALL_FILE} 没过（退出码 {r.returncode}；没合入，脚本应已撤回它装的）：\n{out[-3000:]}",
                       f"看上面的原始输出改 {kind}/{name}/{INSTALL_FILE}（或它装的东西），commit 后再 builder_verify、builder_submit")
        lines.append(f"{kind}/{name}/{INSTALL_FILE}：{out[-300:] or '完成'}")
    return lines


# 改已有工具 / 任务（Joy 的安全目标：允许改 + 审批卡逐项列出；只锁与外部隔离相关的）
TOOL_LOCKED = ("usable_by",)   # 外部内容隔离（只给子代理用的工具）：计划不能改
TOOL_DECLARED = ("network", "docker", "is_editable", "hooks")  # 能改，但要在计划 fields 里逐项写明（卡上列旧值 → 新值），部署时核对
TOOL_WIDEN = ("network", "docker", "hooks")  # 扩大权限的声明：卡上标 ⚠️（hook 是平台执行的文件）
TASK_DECLARED = ("trigger", "schedule", "enabled", "priority", "listen", "listen_events", *COMMAND_KEYS, "runtime", "model", "timeout", "resumable")
TASK_WIDEN = ("listen_events", *COMMAND_KEYS, "runtime", "model")   # 接管事件、宿主上跑的命令与它的联网、换主模型：卡上标 ⚠️


def _get(raw: dict[str, Any], key: str) -> Any:
    """yaml 里的一项；<块>.<项> 取那个映射里的那项。"""
    head, _, sub = key.partition(".")
    v = raw.get(head)
    return (v or {}).get(sub) if sub and isinstance(v or {}, dict) else v if not sub else None


def _show(v: Any) -> str:
    return "（没写）" if v is None else json.dumps(v, ensure_ascii=False)


def _plan_hooks(where: str, raw: Any, guide: str) -> None:
    """计划里的 hooks（新工具 / 新任务的，或改已有任务 fields 的）按 yaml 的写法查（hooks.parse 的规则；脚本还没写，在不在部署时由 load_task / 解析 tool.yaml 查）。"""
    from control_center import hooks

    fix = "写成 {事件: [{command: 文件夹里的脚本文件名, match: [函数名]（只给 pre_call / post_call，可选）, timeout: 60s（可选）}]}，事件只能是 " + "、".join(hooks.EVENTS)
    if not isinstance(raw, dict) or not raw or (bad := sorted(set(raw) - set(hooks.EVENTS))):
        raise _err(f"{where} 的 hooks 写法不对" + (f"：{bad} 不是能写的事件" if isinstance(raw, dict) and raw and bad else ""), fix, guide)  # pyright: ignore[reportPossiblyUnboundVariable]
    for ev, items in raw.items():
        for i, h in enumerate(items if isinstance(items, list) and items else [None], 1):
            at = f"{where} 的 hooks.{ev} 第 {i} 项"
            if not isinstance(h, dict) or "command" not in h or set(h) - set(hooks.HOOK_KEYS):
                raise _err(f"{at} 写法不对", fix, guide)
            if not isinstance(h["command"], str) or "/" in h["command"] or h["command"] in ("", ".", ".."):
                raise _err(f"{at} 的 command {h['command']!r} 不是文件名", "command 写文件夹里的脚本文件名（不带路径），脚本放在工具 / 任务自己的文件夹里", guide)
            match = h.get("match")
            if match is not None and (ev not in hooks.CALL_EVENTS or not isinstance(match, list) or not match or not all(isinstance(x, str) and x for x in match)):  # pyright: ignore[reportGeneralTypeIssues]
                raise _err(f"{at} 的 match 写法不对", "match 只给 pre_call / post_call，写函数名（或工具名）列表，如 [reply]；别的事件不写", guide)
            if "timeout" in h and hooks._seconds(h["timeout"]) is None:
                raise _err(f"{at} 的 timeout {h['timeout']!r} 不合法", "写 60s、5m 这种时长（大于 0），或不写（默认 60 秒）", guide)


def _plan_steps(where: str, steps: Any, models: dict[str, Any], guide: str) -> None:
    """计划里 workflow 的 steps：同任务 yaml 的写法（task.parse_steps；py 还没写，在不在部署时由 load_task 查），写了模型的步骤要对上 models.yaml（含本计划的新档位）的档位。"""
    from control_center.models import profile_for
    from control_center.task import TaskError, parse_steps

    try:
        parsed = parse_steps(steps, None)
    except TaskError as e:
        raise _err(f"{where} 的 steps：{e}", "每步 [编号, 任务文件夹里的 py 文件名, 要不要资源]，要模型的加第 4 项 {runtime, model}", guide) from None
    for st in parsed:
        if st.model is not None and profile_for(models, str(st.runtime), st.model) is None:
            raise _err(f"{where} 第 {st.n} 步的 runtime {st.runtime} + model {st.model} 没有对应的模型档位",
                       "写成 models.yaml 里已有档位的 runtime + model_path；要新模型在 models 里写新档位；只要 GPU / 内存、不要模型服务的不写第 4 项", guide)


def _plan_timeout(where: str, v: Any, guide: str) -> None:
    """计划里的任务 timeout：同任务 yaml（时长或 none）。"""
    from control_center.task import TaskError, parse_duration, unlimited

    try:
        if not unlimited(v):
            parse_duration(v)
    except (TaskError, ValueError, TypeError):
        raise _err(f"{where} 的 timeout {v!r} 不合法", "写 10m、2h、1h30m 这种时长，或 none（不限）", guide) from None


def _hook_lines(hooks: dict[str, Any] | None, who: str) -> list[str]:
    """审批卡：hooks 逐字列出、标 ⚠️（平台在这些时机执行文件夹里的脚本）；没写的不列。"""
    if not hooks:
        return []
    return [f"  ⚠️ hooks（平台在这些时机执行文件夹里的脚本，{who}）：\n```yaml\n{yaml.safe_dump(hooks, allow_unicode=True, sort_keys=False).strip()}\n```"]


def _validate_changes(plan: Plan) -> None:
    """change_tools：已有的仓库工具、is_editable 为 true，fields 只写 TOOL_DECLARED；change_tasks：已有任务，加的工具要存在（或本计划新做），
    去掉的要在任务里，fields 只写 TASK_DECLARED；listen_py 语法与签名对。"""
    from control_center.paths import task_file
    from control_center.registry import TOOLS
    from control_center.task import TaskError, listen_source_functions

    names = [c.name for c in plan.change_tools]
    if len(set(names)) != len(names) or len({c.name for c in plan.change_tasks}) != len(plan.change_tasks):
        raise _err("change_tools / change_tasks 里的名字有重复", "同一个工具 / 任务只写一项，改动合在一起写", "change-tool")
    for c in plan.change_tools:
        spec = TOOLS.get(c.name)
        if spec is None:
            raise _err(f"change_tools 里的 {c.name} 不是已有工具", "写已有工具名（stb tools list 能看到）", "change-tool")
        if not spec.is_editable:
            raise _locked(f"工具 {c.name}", "change-tool")
        if bad := [k for k in c.fields if k in TOOL_LOCKED]:
            raise _err(f"change_tools {c.name} 的 fields 写了 {bad}：usable_by 关系到外部隔离，计划不能改",
                       f"从 fields 去掉 {bad}；真要改请用 ask_user 问 Joy，由他手动改", "change-tool")
        if bad := [k for k in c.fields if k not in TOOL_DECLARED]:
            raise _err(f"change_tools {c.name} 的 fields 写了 {bad}", f"fields 只写这些声明：{', '.join(TOOL_DECLARED)}；description 直接改文件、在 change 里说明", "change-tool")
    for c in plan.change_tasks:
        if not task_file(c.name).exists():
            raise _err(f"change_tasks 里的任务 {c.name} 不存在", "写已有任务名（tasks/ 下的文件夹名）", "change-task")
        if c.name in {k.name for k in plan.tasks} or c.name.startswith(ONEOFF_PREFIX):
            raise _err(f"任务 {c.name} 不能写在 change_tasks（本计划的新任务 / 一次性任务）", "新任务的内容直接写在 tasks 里", "change-task")
        raw = _task_raw(c.name)
        have = _tools_of(raw, c.name)
        if both := set(c.tools_add) & set(c.tools_remove):
            raise _err(f"任务 {c.name}：{sorted(both)} 不能同时加和去掉", "改工具项（guide 等）只写 tools_add（新的工具项），去掉只写 tools_remove", "change-task")
        if bad := [n for n in c.tools_remove if n not in have]:
            raise _err(f"任务 {c.name} 里没有工具 {bad}，去不掉", f"tools_remove 只写任务现有的工具：{', '.join(have)}", "change-task")
        for n, opts in c.tools_add.items():
            if n not in TOOLS and n not in {t.name for t in plan.tools}:
                raise _err(f"任务 {c.name} 要加的工具 {n} 既不是已有工具，也不在本计划里", "写已有工具名，或把它作为新工具写进 tools", "change-task")
            _subagent_only(f"任务 {c.name}", n, plan)
        subs = _subagents_of(raw, c.name)
        if both := set(c.subagents_add) & set(c.subagents_remove):
            raise _err(f"任务 {c.name}：子代理 {sorted(both)} 不能同时加和去掉", "改子代理项（guide）只写 subagents_add（新的子代理项），去掉只写 subagents_remove", "change-task")
        if bad := [n for n in c.subagents_remove if n not in subs]:
            raise _err(f"任务 {c.name} 的 subagents 里没有 {bad}，去不掉", f"subagents_remove 只写任务现有的子代理：{', '.join(subs) or '（没有）'}", "change-task")
        for n in c.subagents_add:
            _subagent_exists(f"任务 {c.name}", n, plan, "change-task")
        if bad := [k for k in c.fields if k not in TASK_DECLARED]:
            raise _err(f"change_tasks {c.name} 的 fields 写了 {bad}",
                       f"fields 只写：{', '.join(TASK_DECLARED)}；tools 用 tools_add / tools_remove，subagents 用 subagents_add / subagents_remove，memory 用 memory，"
                       "system_prompt、description 这类直接改文件", "change-task")
        if c.fields.get("hooks") is not None:
            _plan_hooks(f"change_tasks {c.name}", c.fields["hooks"], "change-task")
        if c.fields.get("timeout") is not None:
            _plan_timeout(f"change_tasks {c.name}", c.fields["timeout"], "change-task")
        steps = c.fields["steps"] if "steps" in c.fields else raw.get("steps")
        if "resumable" in c.fields and (c.fields["resumable"] not in (None, True, False) or (c.fields["resumable"] is not None and steps is None)):
            raise _err(f"change_tasks {c.name} 的 resumable 写了 {c.fields['resumable']!r}",
                       "resumable 写 true / false（null = 删掉，默认 true），且只给 workflow 任务（有 steps 的）", "change-task")
        if c.fields.get("steps") is not None:
            _plan_steps(f"change_tasks {c.name}", c.fields["steps"], plan_models(plan.model_dump()), "workflow-task")
        if steps is not None and any(c.fields.get(x) is not None for x in ("runtime", "model")):
            raise _err(f"change_tasks {c.name} 是 workflow 任务，fields 写了 runtime / model", "workflow 任务不写 runtime / model：要换模型改 steps 里那一步的第 4 项", "workflow-task")
        if steps is None and ("runtime" in c.fields or "model" in c.fields):
            from control_center.models import MAIN_FIELDS, main_missing, profile_for, profiles

            models = plan_models(plan.model_dump())
            rt, md = c.fields.get("runtime", raw.get("runtime")), c.fields.get("model", raw.get("model"))
            prof = profile_for(models, rt, md)  # pyright: ignore[reportArgumentType]
            if prof is None or main_missing(models[prof]):
                chat = [f"{p.get('runtime')} + {p.get('model_path')}" for n, p in profiles(models).items() if not main_missing(p)]
                raise _err(f"change_tasks {c.name}：runtime {rt} + model {md} 不是 models.yaml 里能当主模型的档位（要有 endpoint、{'、'.join(MAIN_FIELDS)}）",
                           f"runtime、model 合起来写成已有能当主模型的档位之一：{'；'.join(chat) or '（没有）'}；要新模型在 models 里写新档位", "change-task")
        if "priority" in c.fields:
            v = c.fields["priority"]
            if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 9:
                raise _err(f"change_tasks {c.name} 的 priority 写了 {v!r}", "写 1–9 的整数（10 只能由 Joy 设）；不改优先级就从 fields 去掉 priority", "change-task")
        if c.listen_py is not None:
            try:
                fns = listen_source_functions(c.listen_py, f"任务 {c.name} 的 listen_py")
            except TaskError as e:
                raise _err(str(e), "改 listen_py 的语法错", "listen") from None
            if bad := [f for f, ok in fns.items() if not ok]:
                raise _err(f"任务 {c.name} 的 listen_py 里 {'、'.join(bad)} 签名不对", "写成 def check(ctx) / def on_event(event, ctx)（普通函数，不是 async）", "listen")
            if "watch" in fns:
                raise _err(f"任务 {c.name} 的 listen_py 里有 watch(ctx, emit)", "去掉 watch，改写 check 或 on_event：builder 写的监听只用 check(ctx)（定时检查）或 on_event(event, ctx)（事件推送）", "listen")


def _plan_untrusted(t: PlanTool) -> None:
    """外部内容工具：计划写 untrusted: true = 读外部内容，tool.yaml 写 usable_by: subagent（只给子代理用，部署时核对），不能加进任务（add_to），
    也不能给 builder 的子代理用（_validate_subagents）；要给隔离子代理（is_editable: false）用由 Joy 手动加进它的 tools。"""
    if t.untrusted and t.add_to:
        raise _err(f"工具 {t.name} 读外部内容（untrusted），只给子代理用，不能加进任务（add_to 写了 {sorted(t.add_to)}）",
                   "去掉 add_to；要让隔离子代理用它，在计划说明里写清，部署后由 Joy 加进那个隔离子代理的 tools", "untrusted-tool")


def _git_files(*paths: str) -> list[str]:
    """仓库（REPO）里 git 跟踪的、在这些路径下的文件。"""
    out = git("ls-files", "-z", "--", *paths, cwd=REPO)
    return sorted(x for x in out.split("\0") if x)


def retire_files(plan: dict[str, Any]) -> dict[str, list[str]]:
    """下线要删的文件（合入提交里的删除），按 REPO 里 git 跟踪的算：键是「工具 <名>」/「任务 <名>」。
    工具：tools/<名>/ 整个；任务：tasks/<名>/ 整个；都加上 tests/stb_platform/test_<名>.py（有的话）。"""
    from control_center.registry import TOOLS

    out: dict[str, list[str]] = {}
    for r in plan.get("retire_tools") or []:
        name, test = r["name"], f"tests/stb_platform/test_{r['name'].replace('-', '_')}.py"
        if name in TOOLS:
            out[f"工具 {name}"] = _git_files(f"tools/{name}", test)
    for r in plan.get("retire_tasks") or []:
        out[f"任务 {r['name']}"] = _git_files(f"tasks/{r['name']}", f"tests/stb_platform/test_{r['name'].replace('-', '_')}.py")
    return out


def retire_cleanup(plan: dict[str, Any]) -> dict[str, dict[str, list[str]]]:
    """下线工具要清掉的运行环境（部署时删、审批卡上逐项列）：工具名 → {"platform": [...]}（以平台用户删，只列现在存在的）：
    有 requirements.txt 的工具的依赖环境 data/runtime/venvs/tools/<名>；data/tool_state/<名>（工具自己的状态目录与已读位置）；
    data/hub/builder-trial/<计划>/ 下这个工具的试跑残留 src/<名>、run/<名>、<名>.stderr.log。"""
    from control_center.registry import TOOLS

    out: dict[str, dict[str, list[str]]] = {}
    for r in plan.get("retire_tools") or []:
        name = r["name"]
        spec = TOOLS.get(name)
        plat = [venv_dir(name), TOOL_STATE / name]
        plat += sorted(p for pat in (f"*/src/{name}", f"*/run/{name}", f"*/{name}.stderr.log") for p in (HUB / "builder-trial").glob(pat))
        out[name] = {"platform": [str(p) for p in plat if p.exists() or p.is_symlink()]}
    return out


def clean_retired(cleanup: dict[str, dict[str, list[str]]]) -> list[str]:
    """部署时按 retire_cleanup 的清单删（合入之后），以平台用户删：解析后必须正好是 data/runtime/venvs/tools/<名>、data/tool_state/<名>、
    data/hub/builder-trial/<计划>/{src,run}/<名> 或 <计划>/<名>.stderr.log。
    不合规则的、删不了的写进返回的说明，不中断部署。"""
    lines = []
    for name, c in cleanup.items():
        done, bad = [], []
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", name):
            lines.append(f"⚠ 没清工具 {name!r} 的运行环境：名字不合法")
            continue
        st, tr, vs = Path(os.path.realpath(TOOL_STATE)), Path(os.path.realpath(HUB / "builder-trial")), Path(os.path.realpath(venv_dir(name).parent))
        for p in c.get("platform") or []:
            real = Path(os.path.realpath(p))
            parts = real.relative_to(tr).parts if real.is_relative_to(tr) else ()
            if not ((real.parent in (st, vs) and real.name == name) or (len(parts) == 3 and parts[1] in ("src", "run") and parts[2] == name)
                    or (len(parts) == 2 and parts[1] == f"{name}.stderr.log")):
                bad.append(f"{p}（解析后不是 data/runtime/venvs/tools/{name}、data/tool_state/{name}、data/hub/builder-trial/<计划>/{{src,run}}/{name} 或 <计划>/{name}.stderr.log，没删）")
                continue
            try:
                shutil.rmtree(real) if real.is_dir() else real.unlink()
                done.append(p)
            except FileNotFoundError:
                done.append(p)
            except OSError as e:
                bad.append(f"{p}（{type(e).__name__}: {e}）")
        if done:
            lines.append(f"已清掉 {name} 的运行环境：{'、'.join(done)}")
        if bad:
            lines.append(f"⚠ {name} 的运行环境有没清掉的：{'；'.join(bad)}")
    return lines


def _tool_refs(name: str) -> dict[str, str]:
    """已有任务里谁在用这个工具：任务名 → tools（写在 tools 里）。"""
    from control_center.paths import task_files, task_name_of

    out: dict[str, str] = {}
    for f in task_files():
        try:
            raw = yaml.safe_load(f.read_text())
            if not isinstance(raw, dict):
                continue
            tools = _tools_of(raw, str(f))
        except (OSError, yaml.YAMLError, BuilderError):
            continue
        task = task_name_of(f)
        if name in tools:
            out[task] = "tools"
    return out


def _validate_retire(plan: Plan) -> None:
    """下线：工具已有、is_editable 不是 false；还在用它的任务要么在 remove_from、要么一起注销；
    子代理（subagents/<名>/subagent.yaml 的 tools）在用的不行；本计划别处不能再用它。任务已有、不是一次性任务，本计划别处不能再改它。"""
    from control_center.paths import task_file
    from control_center.registry import TOOLS

    names = [r.name for r in plan.retire_tools]
    gone = [r.name for r in plan.retire_tasks]
    if len(set(names)) != len(names) or len(set(gone)) != len(gone):
        raise _err("retire_tools / retire_tasks 里的名字有重复", "同一个工具 / 任务只写一项", "retire")
    touched = {k.name for k in plan.tasks} | {c.name for c in plan.change_tasks}
    for r in plan.retire_tasks:
        if not task_file(r.name).exists():
            raise _err(f"retire_tasks 里的任务 {r.name} 不存在", "写已有任务名（tasks/ 下的文件夹名）", "retire")
        if r.name.startswith(ONEOFF_PREFIX):
            raise _err(f"{r.name} 是一次性任务", "不用下线：一次性任务跑完平台自动注销", "retire")
        if r.name in touched:
            raise _err(f"任务 {r.name} 要注销，又写在 tasks / change_tasks 里", f"注销就从 tasks / change_tasks 去掉 {r.name}；要改它就不注销", "retire")
    for r in plan.retire_tools:
        spec = TOOLS.get(r.name)
        if spec is None:
            raise _err(f"retire_tools 里的 {r.name} 不是已有工具", "写已有工具名（stb tools list 能看到）", "retire")
        if not spec.is_editable:
            raise _locked(f"工具 {r.name}", "retire")
        if r.name in {t.name for t in plan.tools} | {c.name for c in plan.change_tools}:
            raise _err(f"工具 {r.name} 要下线，又写在 tools / change_tools 里", f"下线就从 tools / change_tools 去掉 {r.name}", "retire")
        if used := [k.name for k in plan.tasks if r.name in k.tools] + [c.name for c in plan.change_tasks if r.name in c.tools_add]:
            raise _err(f"工具 {r.name} 要下线，本计划的任务 {used} 却要用它", f"从这些任务的 tools / tools_add 去掉 {r.name}", "retire")
        refs = _tool_refs(r.name)
        for task, how in refs.items():
            if task in gone:
                continue
            if task not in r.remove_from:
                raise _err(f"工具 {r.name} 还被任务 {task} 引用（计划没一起撤）",
                           f"在 {r.name} 的 remove_from 里加上 {task}（部署时从它的 tools 撤掉），或把 {task} 一起写进 retire_tasks", "retire")
        for task in r.remove_from:
            if task in gone:
                raise _err(f"任务 {task} 要注销，不用写进 {r.name} 的 remove_from", f"从 remove_from 去掉 {task}", "retire")
            if refs.get(task) != "tools":
                raise _err(f"任务 {task} 的 tools 里没有 {r.name}，撤不了", f"remove_from 只写 tools 里有 {r.name} 的任务：{', '.join(t for t, h in refs.items() if h == 'tools') or '（没有）'}", "retire")
        for p in (subagents.def_path(n) for n in subagents.folder_names()):
            raw = yaml.safe_load(p.read_text()) or {}
            if r.name in [x if isinstance(x, str) else next(iter(x), None) for x in raw.get("tools") or [] if isinstance(x, (str, dict))]:
                raise _err(f"工具 {r.name} 还被子代理 {p.stem} 用着（{p.relative_to(Path(subagents.DIR).parent)} 的 tools）",
                           "不下线它；或先另提计划用 change_subagents 把它从这个子代理的 tools 去掉（只限 is_editable 的子代理；隔离子代理用 ask_user 问 Joy）", "retire")
    files = retire_files(plan.model_dump())
    if empty := [k for k, v in files.items() if not v]:
        raise _err(f"{'、'.join(empty)} 在仓库里没有 git 跟踪的文件", "核对名字；没有文件的不用下线", "retire")


def _retire_lines(plan: Plan) -> list[str]:
    """审批卡：下线逐项列出要从哪些任务撤掉、要注销的任务、要删的文件，都标 ⚠️。"""
    if not (plan.retire_tools or plan.retire_tasks):
        return []
    files, cleanup = retire_files(plan.model_dump()), retire_cleanup(plan.model_dump())
    out = ["\n⚠️ 下线（删除作为合入提交里的删除，部署时执行）："]
    for r in plan.retire_tools:
        clean = cleanup.get(r.name) or {}
        out += [f"  ⚠️ 下线工具 {r.name}：{r.reason}",
                f"    ⚠️ 从这些任务的 tools 里撤掉：{'、'.join(r.remove_from) or '（没有任务在用）'}",
                f"    ⚠️ 删除文件：{'、'.join(files.get(f'工具 {r.name}') or [])}",
                *([f"    ⚠️ 清掉运行环境与残留：{'、'.join(clean['platform'])}"] if clean.get("platform") else ["    运行环境：没有要清的"])]
    for r in plan.retire_tasks:
        out += [f"  ⚠️ 注销任务 {r.name}：{r.reason}",
                f"    ⚠️ 部署时 stb task rm {r.name} --keep-data：注销登记、删定义；运行记录（logs/runs/{r.name}）与工作目录（<workspace>/{r.name}）保留"
                + (f"；删掉它的依赖环境 {venv_dir(r.name, 'tasks')}" if venv_dir(r.name, "tasks").is_dir() else ""),
                f"    ⚠️ 删除文件：{'、'.join(files.get(f'任务 {r.name}') or [])}"]
    return out


def changes_with_retire(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """change_tasks 加上下线带来的「从任务 tools 里撤掉工具」（remove_from）：同一个任务合进它的 tools_remove。"""
    out = {c["name"]: {**c, "tools_remove": list(c.get("tools_remove") or [])} for c in plan.get("change_tasks") or []}
    for r in plan.get("retire_tools") or []:
        for task in r.get("remove_from") or []:
            c = out.setdefault(task, {"name": task, "change": f"下线工具 {r['name']}", "tools_add": {}, "tools_remove": [], "memory": None, "fields": {}})
            if r["name"] not in c["tools_remove"]:
                c["tools_remove"].append(r["name"])
    return list(out.values())


def retire_busy(plan: dict[str, Any]) -> None:
    """要注销的任务正在跑或在队列里：不合入（同 stb task rm 的条件）。"""
    from control_center.runner import task_pending

    gone = {r["name"] for r in plan.get("retire_tasks") or []}
    if not gone:
        return
    busy = sorted(n for n in gone if task_pending(n))
    if busy:
        raise _err(f"要注销的任务 {busy} 正在跑或还在队列里，没合入", "不用改代码：等它跑完再 builder_submit", "retire")


SUBAGENT_LOCKED = ("name", "is_editable", "isolated")   # 改已有子代理时不能动的字段（外部内容隔离只由 Joy 改）
SUBAGENT_WIDEN = ("runtime", "model", "tools", "inputs", "hooks", "timeout", "system_prompt")   # 卡上标 ⚠️：换模型、能用的工具、跑的程序、权限与行为
SUBAGENT_FILE = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}"   # 子代理文件夹里能写的文件名（不进子目录）


def _subagent_config(plan: dict[str, Any], name: str, root: Path | None = None) -> dict[str, Any]:
    """计划里这个子代理部署后的 yaml 内容：新建的就是 config；改的是原文件（root 给了就是 check 仓库里 origin/main 上的那份）+ fields（null = 删掉）。"""
    new = next((x for x in plan.get("subagents") or [] if x["name"] == name), None)
    if new is not None:
        return dict(new["config"])
    c = next(x for x in plan.get("change_subagents") or [] if x["name"] == name)
    rel = f"subagents/{name}/{subagents.YAML_FILE}"
    raw = yaml.safe_load(_base(root, rel) if root is not None else subagents.def_path(name).read_text()) or {}
    for k, v in (c.get("fields") or {}).items():
        raw.pop(k, None) if v is None else raw.__setitem__(k, v)
    return raw


def _validate_subagents(plan: Plan) -> None:
    """新子代理（名字没被占在 validate_plan 里查）：config 按子代理规则校验（文件还没写，server / hooks 的文件部署检查时再查）、
    不写 isolated / is_editable: false（隔离子代理只由 Joy 建）；改的子代理：已有、is_editable 不是 false，SUBAGENT_LOCKED 不动，
    改完照样校验。两种都：换档位的 runtime + model 要对上 models.yaml 或本计划的新档位；agent 的 tools 不能有读外部内容的工具（usable_by: subagent、
    本计划 untrusted 的：只给隔离子代理用）；文件名是文件夹里的普通文件。"""
    from control_center.models import profile_for
    from control_center.registry import TOOLS

    models = plan_models(plan.model_dump())
    known = set(TOOLS) | {t.name for t in plan.tools}
    external = {t.name for t in plan.tools if t.untrusted} | {n for n, t in TOOLS.items() if t.usable_by == "subagent"}
    names = [s.name for s in plan.subagents] + [c.name for c in plan.change_subagents]
    if len(set(names)) != len(names):
        raise _err("subagents / change_subagents 里的名字有重复", "同一个子代理只写一项", "change-subagent")
    for s in plan.subagents:
        if s.config.get("name") != s.name:
            raise _err(f"新子代理 {s.name} 的 config.name 是 {s.config.get('name')!r}", f"config 里写 name: {s.name}", "new-subagent")
        if bad := [k for k in ("isolated",) if k in s.config] + (["is_editable: false"] if s.config.get("is_editable") is False else []):
            raise _err(f"新子代理 {s.name} 写了 {bad}：隔离子代理（读外部内容）只由 Joy 建，builder 建的子代理以后也要能改",
                       "去掉这几项：隔离子代理只能 Joy 建；要读外部内容就让任务调用已有的隔离子代理（stb subagents list 里标了隔离的）", "new-subagent")
        _subagent_files(s.name, s.files, "new-subagent")
    for c in plan.change_subagents:
        if not subagents.is_folder(c.name):
            raise _err(f"change_subagents 里的 {c.name} 不是已有子代理", f"写 subagents/ 下已有的子代理名：{', '.join(subagents.folder_names()) or '无'}", "change-subagent")
        old = yaml.safe_load(subagents.def_path(c.name).read_text()) or {}
        if old.get("is_editable") is False or old.get("isolated"):
            raise _locked(f"子代理 {c.name}", "change-subagent")
        if bad := [k for k in c.fields if k in SUBAGENT_LOCKED]:
            raise _err(f"change_subagents {c.name} 的 fields 写了 {bad}", f"从 fields 去掉 {bad}（{'、'.join(SUBAGENT_LOCKED)} 不能改）", "change-subagent")
        if not c.fields and not c.files:
            raise _err(f"change_subagents {c.name} 什么都没改", "fields 写要改的 yaml 字段，files 写要改的文件夹里的文件", "change-subagent")
        _subagent_files(c.name, c.files, "change-subagent")
    for name in names:
        raw = _subagent_config(plan.model_dump(), name)
        item = "new-subagent" if name in {s.name for s in plan.subagents} else "change-subagent"
        try:
            d = subagents.parse_def(name, raw, tools=known, check_files=False)
        except subagents.SubagentError as e:
            raise _err(f"子代理 {name} 不合格：{e}", f"按报错改（字段见 builder_guide(\"{item}\") 与 stb schema subagents）", item) from None
        if not d.inherit and profile_for(models, d.runtime, d.model) is None:
            raise _err(f"子代理 {name} 的 runtime {d.runtime} + model {d.model} 在 models.yaml 和本计划的 models 里都没有对应档位",
                       "写已有档位的 runtime 与 model_path，或在计划 models 里加这个档位", "model-tier")
        if bad := [t for t in d.tools if t in external]:
            raise _err(f"子代理 {name} 的 tools 里 {bad} 读外部内容（usable_by: subagent / 计划 untrusted），只给隔离子代理用",
                       "从 tools 去掉它们；要读外部内容就让任务调用已有的隔离子代理（任务 yaml 的 subagents）", "untrusted-tool")
        new = next((s for s in plan.subagents if s.name == name), None)
        if d.server is not None and new is not None and d.server.name not in new.files:
            raise _err(f"新子代理 {name} 的程序 {d.server.name} 不在 files 里", f"files 里写上 {d.server.name}（它做什么）", "new-subagent")


def _subagent_files(name: str, files: dict[str, str], item: str) -> None:
    if bad := [f for f in files if not re.fullmatch(SUBAGENT_FILE, f) or f == subagents.YAML_FILE]:
        raise _err(f"子代理 {name} 的 files 写了 {bad}", f"只写文件夹 subagents/{name}/ 里 yaml 以外的普通文件名（不带目录）；yaml 的改动写在 config / fields", item)


def _subagent_line(name: str, opts: Any, plan: Plan | None = None) -> str:
    """审批卡上任务的一个子代理：本计划新建的写种类，已有的写它 yaml 的 description；有子代理项（guide）就列出。"""
    new = next((s for s in plan.subagents if s.name == name), None) if plan else None
    if new is not None:
        what = f"本计划新建（{new.config.get('kind')}）"
    else:
        try:
            what = subagents.load_def(name).description
            what = what if len(what) <= 200 else what[:200] + "…"
        except subagents.SubagentError:
            what = "（读不出来）"
    return f"    · {name}：{what}" + (f"\n      选项：{json.dumps(opts, ensure_ascii=False)}" if opts else "")


def _subagent_lines(plan: Plan) -> list[str]:
    """审批卡：新子代理的 yaml 全文与文件，改的子代理逐项旧值 → 新值与要改的文件；要换模型的标 ⚠️「调用时会卸下主模型，换成 <档位>（约 N GB）」，
    程序以 sparky + Landlock 跑。"""
    from control_center.models import profile_for
    from control_center.registry import TOOLS

    models = plan_models(plan.model_dump())
    out: list[str] = []

    def switch(raw: dict[str, Any]) -> list[str]:
        if raw.get("runtime") == subagents.INHERIT:
            return ["  模型：同调用它的任务（不换模型）"]
        tier = profile_for(models, str(raw.get("runtime")), str(raw.get("model")))
        return [f"  ⚠️ 调用时会卸下主模型，换成 {tier}（约 {(models.get(tier) or {}).get('min_avail_gb')} GB），期间主模型等着；调用完换回"]  # pyright: ignore[reportArgumentType]

    def tools(raw: dict[str, Any]) -> list[str]:
        return ([f"  ⚠️ 能用的工具：{'、'.join(raw['tools']) or '（没有）'}"] + [f"    · {t}：{TOOLS[t].description.strip()[:150]}" for t in raw["tools"] if t in TOOLS]
                if isinstance(raw.get("tools"), list) else [])

    for s in plan.subagents:
        out += [f"\n新子代理 {s.name}（{s.config.get('kind')}，文件夹 subagents/{s.name}/；任务的 subagents 写它，主模型就能调用）", *switch(s.config), *tools(s.config),
                *([f"  ⚠️ 程序 {subagents.SERVER_FILE}：以 sparky 跑（文件看 sparky 的文件权限，成品写本次运行的 media，只连它模型档位的端口）"] if s.config.get("kind") in ("generator", "engine") else []),
                "  yaml 全文：", *("    " + line for line in yaml.safe_dump(s.config, allow_unicode=True, sort_keys=False).splitlines()),
                "  其他文件：" + ("；".join(f"{f}（{w}）" for f, w in s.files.items()) or "无")]
    for c in plan.change_subagents:
        old = yaml.safe_load(subagents.def_path(c.name).read_text()) or {}
        new = _subagent_config(plan.model_dump(), c.name)
        out += [f"\n修改已有子代理 {c.name}：{c.change}", *switch(new), *(tools(new) if "tools" in c.fields else []),
                *_field_lines(old, c.fields, SUBAGENT_WIDEN, "、".join(SUBAGENT_LOCKED) + " 不能改"),
                *([f"  ⚠️ 改文件夹里的文件：{'；'.join(f'{f}（{w}）' for f, w in c.files.items())}"] if c.files else [])]
    return out


def _tools_of(raw: dict[str, Any], where: str, key: str = "tools") -> dict[str, Any]:
    """任务 yaml 原文的 tools（key 写 subagents 就是 subagents）→ {名字: 项}。"""
    tools = {}
    for item in raw.get(key) or []:
        name, opts = (item, {}) if isinstance(item, str) else next(iter(item.items())) if isinstance(item, dict) and len(item) == 1 else (None, None)
        if name is None:
            raise BuilderError(f"{where} 的 {key} 条目格式不对：{item!r}")
        tools[str(name)] = opts or {}
    return tools


def _subagents_of(raw: dict[str, Any], where: str) -> dict[str, Any]:
    """任务 yaml 原文的 subagents → {子代理名: 子代理项}。"""
    return _tools_of(raw, where, "subagents")


def tool_files(name: str) -> list[str]:
    """已有工具在仓库里的位置（计划改它时能改的范围）：工具文件夹 tools/<名>/ 里的文件；第一项是 tool.yaml。"""
    return [f"tools/{name}/tool.yaml", f"tools/{name}/"]


def _plan_decls(t: PlanTool) -> None:
    """计划工具的 docker 声明：按 tool.yaml 的规则校验（资源上限）。"""
    from control_center import registry

    try:
        if t.docker is not None:
            registry._docker(Path(f"计划工具 {t.name}"), t.docker)
    except ValueError as e:
        raise BuilderError(str(e)) from None


def _tool_line(name: str, opts: Any, plan: Plan | None = None) -> str:
    """审批卡上新任务的一个工具：做什么与风险备注（tool.yaml 的 description）+ 工具项（guide 等）；本计划新做的写联网与凭证。"""
    from control_center.registry import TOOLS

    new = next((t for t in plan.tools if t.name == name), None) if plan else None
    if new is not None:
        what = f"本计划新做；联网：{'是' if new.network else '否'}" + ("；⚠️ 读外部内容（只给子代理用）" if new.untrusted else "")
    elif name in TOOLS:
        what = TOOLS[name].description.strip()
        what = what if len(what) <= 200 else what[:200] + "…"
    else:
        what = "（未登记）"
    return f"    · {name}：{what}" + (f"\n      选项：{json.dumps(opts, ensure_ascii=False)}" if opts else "")


def _command_lines(k: Any) -> list[str]:
    """审批卡：workflow 任务的步骤逐个列出（以 sparky 在宿主上跑，GPU 直接用，能读凭证），联网；都标 ⚠️。"""
    if k.steps is None:
        return []
    out = ["  ⚠️ 步骤（以 sparky 在宿主上跑任务文件夹里的 py，不进容器，GPU 直接用，能读凭证、能读写 sparky 能碰的文件）："]
    for st in k.steps:
        res = (f"要资源，模型 {st[3]['runtime']} + {st[3]['model']}" if len(st) > 3 and isinstance(st[3], dict) else "要资源，不起模型") if len(st) > 2 and st[2] else "不要资源（旁路马上跑）"
        out.append(f"    {st[0]}. {st[1]}：{res}")
    out.append(f"  {'⚠️ ' if k.network else ''}步骤联网：{'是' if k.network is True else k.network if k.network else '否'}")
    return out


def render_plan(pid: str, plan: Plan, head: bool = True) -> str:
    """计划的文字版。head=False 不写开头的计划号与目的（审批卡的第一行已经写了）。"""
    out = [f"计划 {pid}", f"目的：{plan.purpose}"] if head else []
    for t in plan.tools:
        out += [f"\n新工具 {t.name}（联网：{'是' if t.network else '否'}）", f"  函数：{t.functions}",
                f"  依赖：{', '.join(t.deps) or '无'}",
                f"  部署后加给：{json.dumps(t.add_to, ensure_ascii=False) if t.add_to else '不加（要用得手动写进任务 yaml）'}",
                *([f"  容器：{json.dumps(t.docker, ensure_ascii=False)}"] if t.docker else []),
                *([f"  ⚠️ 读外部内容：输出是别人写的自由文本，只给子代理用（usable_by: subagent），主模型拿不到原文；要给隔离子代理用由 Joy 加进它的 tools"] if t.untrusted else []),
                *_hook_lines(t.hooks, "以 sparky 跑，联网同这个工具"),
]
    for k in plan.tasks:
        how = f"模型 {k.runtime} + {k.model}" if k.steps is None else "workflow（按步骤跑）"
        out += [f"\n新任务 {k.name}：{k.description}", f"  触发：{_trigger_text(k)}，优先级 {k.priority}，"
                f"{how}，时限 {k.timeout if k.timeout is not None else '开发时定'}，"
                f"试跑成功后{'上线' if k.enabled_after_deploy else '保持停用'}",
                *_listen_role_lines(k.name, k.listen, k.listen_py is not None, plan),
                *_listen_lines(k.name, k.listen_py, k.listen_events, k.network),
                "  工具（每个能做什么）：",
                *(_tool_line(name, opts, plan) for name, opts in k.tools.items()),
                *(["  子代理（主模型能调用的）：", *(_subagent_line(name, opts, plan) for name, opts in k.subagents.items())] if k.subagents else []),
                *_command_lines(k),
                *([f"  步骤中间可抢占 / 暂停（resumable）：{'是' if k.resumable is not False else '否'}"] if k.steps is not None else []),
                *_hook_lines(k.hooks, "以 sparky 跑，联网同任务的 network"),
                *([f"  依赖：{', '.join(k.deps)}（装进它自己的依赖环境，和平台的依赖一起装）"] if k.deps else []),
                f"  读记忆：{'、'.join(k.memory) + '（每次运行注入全文，只读）' if k.memory else '否'}"]
    for c in plan.change_tools:
        old = yaml.safe_load((REPO / tool_files(c.name)[0]).read_text()) or {}
        out += [f"\n修改已有工具 {c.name}（能改的文件：tools/{c.name}/ 里的文件）", f"  改什么：{c.change}",
                *_field_lines(old, c.fields, TOOL_WIDEN, "usable_by 不能改"), *_deps_lines(REPO / "tools" / c.name, c.deps)]
    for c in plan.change_tasks:
        raw = _task_raw(c.name)
        rest = {k: v for k, v in c.fields.items() if k != "priority"}
        from control_center.paths import task_file

        own = task_file(c.name).parent / "listen.py"
        out += [f"\n修改已有任务 {c.name}：{c.change}",
                *(_listen_role_lines(c.name, c.fields.get("listen", raw.get("listen")), c.listen_py is not None or own.is_file(), plan)
                  if "listen" in c.fields or c.listen_py is not None else []),
                *([f"  ⚠️ 优先级 {_show(raw.get('priority', 5))} → {_show(c.fields['priority'])}（优先级平常由你定，理由见上面「改什么」）"] if "priority" in c.fields else []),
                *(_field_lines(raw, rest, TASK_WIDEN, "fields 以外的声明不变") if rest or "priority" not in c.fields else []),
                *(["  ⚠️ 改了宿主上跑的声明：这个任务的步骤与 hook 以 sparky 跑（联网按 network）"]
                  if set(c.fields) & set(COMMAND_KEYS) else []),
                *([f"  ⚠️ 换主模型：{raw.get('runtime')} + {raw.get('model')} → {c.fields.get('runtime', raw.get('runtime'))} + {c.fields.get('model', raw.get('model'))}"]
                  if "runtime" in c.fields or "model" in c.fields else []),
                *(["  ⚠️ 工具增加 / 改工具项（等于加权限，逐个看）：", *(_tool_line(name, opts, plan) for name, opts in c.tools_add.items())] if c.tools_add else []),
                *([f"  工具去掉：{'、'.join(c.tools_remove)}"] if c.tools_remove else []),
                *(["  ⚠️ 子代理增加 / 改子代理项：", *(_subagent_line(name, opts, plan) for name, opts in c.subagents_add.items())] if c.subagents_add else []),
                *([f"  子代理去掉：{'、'.join(c.subagents_remove)}"] if c.subagents_remove else []),
                *_listen_lines(c.name, c.listen_py, c.fields.get("listen_events", raw.get("listen_events") or []),
                               c.fields.get("network", raw.get("network")), changed=True),
                *_deps_lines(task_file(c.name).parent, c.deps),
                f"  读记忆：{'不变' if c.memory is None else '改为 ' + '、'.join(c.memory) + '（每次运行注入全文，只读）' if c.memory else '改为不读'}"]
    out += _retire_lines(plan)
    out += _subagent_lines(plan)
    for m in plan.models:
        out += [f"\n新模型档位 {m.name}（部署时写进 models.yaml）",
                *(f"  {k}：{v}" for k, v in m.profile.items())]
    return "\n".join(out)


def _deps_lines(folder: Path, deps: list[str] | None) -> list[str]:
    """审批卡：改已有工具 / 任务的依赖「旧 → 新」（部署时按新的重建依赖环境）；计划没写 deps 的不列。"""
    from control_center.registry import REQUIREMENTS_FILE, _requirements

    if deps is None:
        return []
    return [f"  依赖（requirements.txt）：{', '.join(_requirements(folder / REQUIREMENTS_FILE)) or '无'} → {', '.join(deps) or '无（改用平台的 venv）'}"
            "（部署时重建它的依赖环境，和平台的依赖一起装）"]


def _field_lines(old: dict[str, Any], fields: dict[str, Any], widen: tuple[str, ...], locked: str) -> list[str]:
    """审批卡：声明改动逐项「旧值 → 新值」，扩大权限的标 ⚠️。"""
    if not fields:
        return [f"  声明不变（{locked}）"]
    return [f"  声明改动（旧值 → 新值；{locked}）：",
            *(f"    {'⚠️' if k in widen else '·'} {k}：{_show(_get(old, k))} → {_show(v)}" for k, v in fields.items())]


def proposer_of(run: str) -> str:
    """提案的任务名（run 是 <任务>/<运行 id>）。"""
    return run.split("/", 1)[0] if run else ""


# ---------- 提案（主模型调用子代理 builder 时的 prompt；subagents/builder/proposal_hook.py 在 pre_call 里调 review_proposal / accept） ----------
PROPOSAL_KEYS = ("plan", "oneoff")      # 提案顶层只写其中一个：plan = 开发计划（Plan），oneoff = 一次性任务（OneOff）
PROPOSAL_FIX = "prompt 写 YAML 提案（可以放在 ```yaml 围栏里），顶层正好写 plan:（开发计划）或 oneoff:（一次性任务）其中一个；格式见第一次调用 builder 时返回的说明"
YAML_FIX = "值里有「: 」「 #」，或以 ` * & ! [ { 之类开头的，改用块写法（键: >- 换行，正文缩进两格写）；" + PROPOSAL_FIX


def parse_proposal(prompt: str) -> tuple[str, dict[str, Any]]:
    """prompt → (plan | oneoff, 内容)。prompt 里有 ```yaml 围栏就只取第一个围栏里的；围栏外还有文字就退回（卡上只显示提案，外面的话 Joy 看不到）。
    漏写了顶层 plan: / oneoff:、顶层的键又只能是其中一种的字段（如 change_tasks 只有开发计划有），就当作那一种。"""
    text = str(prompt or "")
    if m := re.search(r"```(?:ya?ml)?[ \t]*\n(.*?)```", text, re.S):
        if (text[:m.start()] + text[m.end():]).strip():
            raise _err("提案的 YAML 外面还有文字：审批卡只显示 YAML 里的内容，外面的话 Joy 看不到",
                       "prompt 里只写 YAML 提案；要 Joy 看到的说明（如照他的意见改了什么）写进 purpose，再调用")
        text = m.group(1)
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise _err(f"提案不是合法的 YAML：{str(e)[:300]}", YAML_FIX) from None
    if isinstance(raw, dict) and raw and not set(raw) & set(PROPOSAL_KEYS):
        fits = [k for k, m in (("plan", Plan), ("oneoff", OneOff)) if set(raw) <= set(m.model_fields)]
        if len(fits) == 1:
            raw = {fits[0]: raw}
    if not isinstance(raw, dict) or [k for k in PROPOSAL_KEYS if k in raw] == [] or len([k for k in PROPOSAL_KEYS if k in raw]) > 1 or set(raw) - set(PROPOSAL_KEYS):
        got = sorted(raw) if isinstance(raw, dict) else type(raw).__name__
        raise _err(f"提案顶层写的是 {got}", PROPOSAL_FIX)
    kind = next(k for k in PROPOSAL_KEYS if k in raw)
    if not isinstance(raw[kind], dict):
        raise _err(f"提案的 {kind} 要写成映射", PROPOSAL_FIX)
    return kind, raw[kind]


def _validated(model: Any, raw: dict[str, Any], what: str) -> Any:
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise _err(f"{what}格式不对：" + "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors()),
                   "按上面每一项的位置（如 tasks.0.trigger）和原因改提案") from None


def explain(e: BuilderError) -> str:
    """提案被拒的说明交回主模型：报错 + 对应规范的全文（主模型没有 builder_guide）。"""
    return str(e) + (f"\n\n{guide_text(e.topic)}" if e.topic in TOPICS else "")


def review_proposal(prompt: str, run: str) -> dict[str, Any]:
    """校验提案，返回审批卡的内容 {kind, key, action, detail, …}；同一份开发计划已批准、开发区已就绪（续写同一个计划）返回 {"ready": 计划 id}，不再发卡。
    不合格抛 BuilderError（带改法）。"""
    kind, body = parse_proposal(prompt)
    key = f"{kind}:" + hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:40]
    pid = time.strftime("%m%d") + secrets.token_hex(3)
    if kind == "oneoff":
        o = _validated(OneOff, body, "一次性任务")
        name, text = oneoff_yaml(o, proposer_of(run))
        validate_oneoff(o, text, _dir() / pid / f"{name}.yaml")
        tools = "\n".join(_tool_line(n, opts) for n, opts in o.tools.items()) or "  （没有工具）"
        subs = "".join(f"\n{_subagent_line(n, opts)}" for n, opts in o.subagents.items())
        doc = yaml.safe_load(text)
        detail = (f"批准后建好并在后台跑一次，跑完自动删除\n优先级 {o.priority}；模型 {doc['runtime']} + {doc['model']}\n"
                  f"工具（每个能做什么）：\n{tools}\n" + (f"子代理（每个能做什么）：{subs}\n" if subs else "") + f"\n任务说明：\n{o.system_prompt.strip()}")
        return {"kind": kind, "key": key, "pid": pid, "task": name, "purpose": o.purpose, "yaml": text,
                "action": f"建一次性任务 {name}：{o.purpose}", "detail": detail}
    plan = _validated(Plan, body, "计划")
    old = active_plan()
    if old and old.get("key") == key and old["status"] in ("approved", "preparing", "ready"):
        return {"ready": old["id"]}
    if old and old["status"] in ("deploying", "merged"):
        raise _err(f"计划 {old['id']} 正在 {old['status']}", "等它部署完（结果会私信 Joy）再提新计划")
    validate_plan(plan)
    detail = "批准后 builder 自己开发、测试、部署，不再二次确认" + render_plan(pid, plan, head=False)   # 计划号与目的写在卡的第一行（action）
    return {"kind": kind, "key": key, "pid": pid, "plan": plan.model_dump(), "action": f"开发计划 {pid}：{plan.purpose}", "detail": detail}


def accept(r: dict[str, Any], run: str) -> tuple[bool, str]:
    """Joy 批准了提案 r（review_proposal 的返回）：开发计划建开发区（prepare）→ 放行（True）；一次性任务建好并入队 → 拦下（False），结果交回主模型。"""
    if r["kind"] == "oneoff":
        p = {"id": r["pid"], "kind": "oneoff", "task": r["task"], "purpose": r["purpose"], "yaml": r["yaml"], "proposer": proposer_of(run), "key": r["key"],
             "created": time.time()}
        save_plan(p, "approved")
        return False, start_oneoff(p)
    if (old := active_plan()) and old["status"] in ("approved", "preparing", "ready"):
        end_plan(old, "superseded")                    # 同一时刻只有一个开发计划：新批准的取代没部署的旧计划
    p = {"id": r["pid"], "plan": r["plan"], "proposer": proposer_of(run), "key": r["key"], "created": time.time()}
    doc = _dir() / p["id"] / f"plan-{p['id']}.md"
    doc.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    doc.write_text(r["detail"] + "\n\n```json\n" + json.dumps(r["plan"], ensure_ascii=False, indent=1) + "\n```\n")
    save_plan(p, "approved")
    return prepare(p["id"])


def rejected_text(value: str | None) -> str:
    """「拒绝，你应该…」：写了意见 = 照改重提；没写 = 整个不要。"""
    if value:
        return f"Joy 拒绝了，他说：「{value[:1000]}」。照他的意见改提案，再调用 builder（会发一张新卡）"
    return "Joy 拒绝了这个提案（没写意见）：整个不要。告诉 Joy 不做了"


class OneOff(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,30}$", description="任务名（小写字母、数字、连字符）；平台自动加前缀 oneoff-")
    purpose: str = Field(min_length=1, max_length=500, description="目的：要做成什么（给 Joy 看）")
    system_prompt: str = Field(min_length=1, max_length=8000, description="任务说明：后台 agent 照它做（它看不到这次私信，要写全）")
    tools: dict[str, dict[str, Any]] = Field(default_factory=dict, description="要用的已有工具：工具名 → 工具项（一般写 {}）；平台不自动加工具，要私信 Joy 结果就列 reply")
    subagents: dict[str, dict[str, Any]] = Field(default_factory=dict, description="要用的已有子代理：子代理名 → 子代理项（一般写 {}），同任务 yaml 的 subagents")
    runtime: str = Field(default="", description="不写 = 同提交它的任务")
    model: str = Field(default="", description="不写 = 同提交它的任务")
    priority: int = Field(default=5, ge=1, le=9)


def _task_raw(name: str) -> dict[str, Any]:
    from control_center.paths import task_file

    try:
        raw = yaml.safe_load(task_file(name).read_text())
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def oneoff_yaml(o: OneOff, proposer: str) -> tuple[str, str]:
    """一次性任务的 yaml：trigger manual，工具、子代理就是提交时列的那些；runtime / model 不写就继承提交它的任务（inherit）。
    沙箱、工作目录（保留）、运行结束通知 Joy 都是平台统一的，不写。"""
    dm = _task_raw(proposer)
    name = o.name if o.name.startswith(ONEOFF_PREFIX) else ONEOFF_PREFIX + o.name
    doc = {"name": name, "description": o.purpose, "enabled": True, "trigger": "manual", "timeout": "none",
           "priority": o.priority, "runtime": o.runtime or dm.get("runtime"), "model": o.model or dm.get("model"),
           "tools": [{k: v} if v else k for k, v in o.tools.items()],
           **({"subagents": [{k: v} if v else k for k, v in o.subagents.items()]} if o.subagents else {}),
           "system_prompt": o.system_prompt.rstrip() + "\n"}
    head = f"# builder 一次性任务（{time.strftime('%Y-%m-%d %H:%M')}）：跑完一次自动注销，运行记录留在 logs/runs/{name}/\n"
    return name, head + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=1000)


def validate_oneoff(o: OneOff, text: str, where: Path) -> None:
    """只能用已有工具和子代理（不能新建）；再用 load_task 完整校验（工具项、子代理项、模型档位等）。"""
    from control_center.models import load_models
    from control_center.paths import task_file
    from control_center.registry import TOOLS, refresh
    from control_center.runner import load_registered
    from control_center.task import TaskError, load_task

    refresh()
    for name in o.tools:
        if name not in TOOLS:
            raise _err(f"工具 {name} 不是已有工具", "一次性任务只能用已有工具；要新工具提开发计划（提案写 plan:）")
    for name in o.subagents:
        if not subagents.is_folder(name):
            raise _err(f"子代理 {name} 不是已有子代理", "一次性任务只能用已有子代理；要新子代理提开发计划（提案写 plan:）")
    name = yaml.safe_load(text)["name"]
    if task_file(name).exists() or name in load_registered():
        raise BuilderError(f"任务 {name} 已存在，换个名字")
    where.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    where.write_text(text)
    try:
        load_task(where, load_models(), refresh=False)
    except (TaskError, ValueError) as e:
        raise _err(f"一次性任务校验失败：{e}", "按报错改提案 oneoff 里对应的字段（tools 的工具项、subagents 的子代理项、模型档位、时长写法…），改好再调用 builder") from None


def start_oneoff(p: dict[str, Any]) -> str:
    """批准一次性任务：写 tasks/oneoff-<名>/task.yaml → load_task 校验 → 登记 → 入队跑一次。"""
    from control_center.models import load_models
    from control_center.paths import task_file
    from control_center.runner import enqueue, mark_registered
    from control_center.task import TaskError, load_task

    name, f = p["task"], task_file(p["task"])
    if f.exists():
        save_plan(p, "failed", f"任务 {name} 已存在")
        return f"已批准，但任务 {name} 已存在，没建：换个名字重新提案"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(p["yaml"])
    try:
        load_task(f, load_models())
    except (TaskError, ValueError) as e:
        shutil.rmtree(f.parent, ignore_errors=True)
        save_plan(p, "failed", str(e))
        return f"已批准，但任务校验没过，没建：{e}"
    mark_registered(name)
    enqueue(name, trigger="manual")
    save_plan(p, "queued")
    return f"已批准：已建好并入队 {name}，结果会由这个任务私信 Joy，跑完自动删除。告诉 Joy 这些，这轮就结束"


def remove_oneoff(name: str) -> None:
    """一次性任务跑完：同 stb task rm 注销（删任务文件夹、登记、工作目录），运行记录留着。"""
    from control_center.cli import remove_task

    if name.startswith(ONEOFF_PREFIX):
        try:
            remove_task(name, keep_runs=True)
        except OSError as e:
            print(f"注销一次性任务 {name} 失败：{e}", flush=True)




# ---------- 凭证卡 ----------
def _cred_plan() -> dict[str, Any]:
    """凭证卡要有已批准的计划（开发、部署中或部署过）：工具读凭证是用到时 credential("KEY")，计划里不再列键名。"""
    hits = [p for f in (_dir() / "plans").glob("*.json") if (p := _read_json(f, {})).get("status") in ("ready", "deploying", "merged", "deployed", "failed")]
    if not hits:
        raise BuilderError("现在没有已批准的 builder 计划，不能发凭证卡")
    return max(hits, key=lambda p: p.get("created", 0))


async def request_credential(key: str, note: str, run: str = "") -> dict[str, Any]:
    """builder_request_credential：给 Joy 发一张凭证卡（只有他能填，值经中枢内存直接写进 .env，不经过 agent）；不等他填。"""
    from control_center import hub_client

    if env.get(key):
        raise BuilderError(f"{key} 在 .env 里已经有了：凭证卡只给 .env 还没有的键发（原有的凭证不经卡改写）")
    p = _cred_plan()
    body = (f"计划 {p['id']} 开发的工具要用 {key}。\nagent 说：{note.strip()[:300] or '（没写）'}\n"
            + "点「填写」亲自输入：值直接写进 .env，不经过 agent，卡片和日志里都不留")
    r = await hub_client.call("card", kind=cards.ENV_KIND, title=f"🔑 builder 要凭证 {key}", body=body,
                                  buttons=[{"id": "enter", "label": "填写", "style": "primary", "modal": True},
                                           {"id": "reject", "label": "不给", "style": "danger"}],
                                  modal={"title": key[:45], "fields": [{"id": "password", "label": "值（直接写进 .env，不会记录）", "secret": True}]},
                                  payload={"plan": p["id"], "key": key, **({"run": run} if run else {})})
    if not (r and r.get("ok")):
        raise BuilderError(f"凭证卡没发出去（stbcc-hub 不在？）：{(r or {}).get('error', '')}")
    return {"card": r.get("id"), "note": "凭证卡已发给 Joy（只有他能填，值不经过你）。builder_status 的 credentials 能看到填没填（列的是本计划工具代码里 credential(\"…\") 读的键）；"
                                         "开发中的计划填好再 builder_verify / builder_submit，部署过的用 builder_check 验证"}


def _handle_env_card(card: dict[str, Any], action: str, _value: Any) -> str:
    """凭证卡处理器（stbcc-hub 进程里，各入口经 card_act 交中枢）：「填写」→ 取中枢内存里的值，写进 .env；处理结果里不带值。
    卡有两种来源：builder 计划要的（builder_request_credential，payload 带 plan），工具调用时缺的（网关发，payload 带 tool）。"""
    v = (cards.take_secret(card["id"]) or "").strip() if action == "enter" else None
    if v is None:
        return "Joy 没给这个凭证"
    key = str(card.get("payload", {}).get("key", ""))
    if not v or "\n" in v or "\r" in v:
        return f"没写进 .env：{key} 的值是空的或有换行"
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,60}", key):
        return f"没写进 .env：键名 {key!r} 不合法"
    if "plan" in (card.get("payload") or {}):          # builder 计划要的：计划还在；工具调用时缺的（网关发的卡，payload 带 tool）只看键名
        try:
            _cred_plan()
        except BuilderError as e:
            return f"没写进 .env：{e}"
    env.update({key: v})
    return f"已把 {key} 写进 .env（值不记录）"


cards.register(cards.ENV_KIND, _handle_env_card)


# ---------- 凭证检查 ----------
async def check_tool(p: dict[str, Any], name: str) -> tuple[bool, str]:
    """像网关那样（sparky 用户、计划的联网、读得到 .env 的凭证）起已部署的工具，调一次 <工具名>_check；返回（过没过，输出）。"""
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from control_center import registry

    registry.refresh(REPO)
    work = _dir() / p["id"] / "checkwork"
    work.mkdir(parents=True, exist_ok=True, mode=0o750)

    async def go() -> tuple[bool, str]:
        cmd, envs = agent_command(registry.TOOLS[name], work, "check")
        async with stdio_client(StdioServerParameters(command=cmd[0], args=cmd[1:], env=envs)) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool(f"{name}_check", {})
                return not res.is_error, "".join(getattr(c, "text", "") for c in res.content)[:500]

    try:
        return await asyncio.wait_for(go(), CHECK_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — 起不来、超时、凭证错都算没过
        return False, f"{type(e).__name__}: {str(e)[:300]}"


# ---------- 真实运行方式下试跑（开发中 builder_try_tool、合入前部署自动做） ----------
TRIAL_TIMEOUT_S = 300                  # 工具试跑（起工具 + 调一次）的上限；计划给工具写了 docker.timeout 的按它加 TRIAL_MARGIN_S（trial_timeout）
TRIAL_MARGIN_S = 300
_TRIAL_LOCK = threading.Lock()         # 预演目录与试跑目录一次只给一个用：同一工具的两次试跑并发时，后一次的 rm -rf 会删掉前一次刚建的代码目录（tar 报 Cannot open）
TRIAL_TASK = "builder-trial"           # 试跑的运行 ref 的前半：builder-trial/<计划>（同 docker_service.TRIAL_TASK）
TRY_SUBAGENT = "builder_try_subagent"  # builder-dev 的函数：stbcc-builder 装好预演副本（stage_subagent），网关接着按正式调用真跑（gateway._try_subagent）


def trial_timeout(t: dict[str, Any]) -> tuple[float, str]:
    """计划里一个工具的试跑上限（秒）与来由：docker.timeout（容器的预计时长，如加载模型再跑）加 TRIAL_MARGIN_S；没写就是 TRIAL_TIMEOUT_S。"""
    from control_center.task import TaskError, parse_duration

    raw = (t.get("docker") or {}).get("timeout")
    try:
        sec = float(parse_duration(raw)) if raw else None
    except TaskError:
        sec = None
    if sec is None:
        return float(TRIAL_TIMEOUT_S), f"工具没写 docker.timeout，用默认 {TRIAL_TIMEOUT_S} 秒"
    return sec + TRIAL_MARGIN_S, f"计划的 docker.timeout {raw} 加 {TRIAL_MARGIN_S} 秒余量"


def _trial_root(pid: str) -> Path:
    """试跑用的运行目录与工作目录（data 下，sparky 能读；写权限由 media 目录与本工具的运行目录 <名>/ 单独开）。"""
    d = HUB / TRIAL_TASK / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stage_home(pid: str) -> Path:
    """预演目录（合入前试跑用，代码、venv、状态各一份）：/home/sparky/stage/<计划>/{tools,venvs,state}/<名>。"""
    return SPARKY_HOME / "stage" / pid


def _install_tool(pid: str, name: str, deps: list[str], src: Path, home: Path, fresh_venv: bool = True) -> None:
    """预演：src（joyzhang14 能读、已拒掉链接的代码目录）拷成 home/tools/<名>，有依赖的在断网容器里用 wheelhouse 离线建 home/venvs/<名>（_build_venv），建 home/state/<名>
    （home 是预演目录，全归 sparky）。fresh_venv=False 时 venv 已在就不重建（依赖只由计划定，同一计划里不变）。"""
    code, venv, state = (str(home / d / name) for d in ("tools", "venvs", "state"))
    sudo("rm", "-rf", code)
    if fresh_venv:
        sudo("rm", "-rf", venv)
    sudo("mkdir", "-p", code, venv, state + "/tmp")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(src, arcname=".")
    sudo("tar", "-x", "-C", code, "-f", "-", input=buf.getvalue())
    if not deps or (not fresh_venv and _run(["sudo", "-n", "-u", SPARKY, "--", "test", "-x", f"{venv}/bin/python"]).returncode == 0):
        return
    _build_venv(pid, venv, DEV_USER, deps)


def _install_venv(pid: str, name: str, deps: list[str], kind: str = "tools") -> None:
    """上线：断网容器里（以平台用户）用 wheelhouse 离线重建 data/runtime/venvs/<tools|tasks>/<名>（平台依赖 + 计划依赖；归平台用户，sparkle 组只读）。
    代码不拷：运行时就用仓库里的 tools/<名>/、tasks/<名>/。"""
    venv = venv_dir(name, kind)
    shutil.rmtree(venv, ignore_errors=True)
    venv.mkdir(parents=True)
    _build_venv(pid, str(venv), f"{os.getuid()}:{os.getgid()}", deps)


def _build_venv(pid: str, venv: str, user: str, deps: list[str]) -> None:
    """断网容器里用计划的 wheelhouse 建依赖环境：平台依赖的锁定版本（PLATFORM_REQS）和计划依赖交给 pip 一起解析安装（冲突就报错、不合入），
    再写一个 .pth 指向仓库根，环境里照样能 import 平台的 control_center。"""
    wh = str(_dir() / pid / "wheelhouse")
    base = {"image": DEV_IMAGE, "user": user, "env": {"HOME": "/tmp"}, "tmpfs": [{"path": "/tmp", "size": "1g", "exec": True}],
            "mounts": [{"host": venv, "container": venv, "mode": "rw"}, {"host": wh, "container": "/wheelhouse", "mode": "ro"}]}
    _ok(_drun({**base, "argv": ["python3", "-m", "venv", venv]}, timeout=300), "建 venv")
    _ok(_drun({**base, "argv": [f"{venv}/bin/pip", "install", "-q", "--no-cache-dir", "--no-index", "--find-links", "/wheelhouse",
                                "-r", f"/wheelhouse/{PLATFORM_REQS}", *deps]}, timeout=900), "装依赖（平台的依赖与计划依赖一起装，版本冲突看上面 pip 的报错）")
    pth = str(REPO) + "\n"
    _ok(_drun({**base, "argv": [f"{venv}/bin/python", "-c", "import site, sys; open(site.getsitepackages()[0] + '/stb-platform.pth', 'w').write(sys.argv[1])", pth]},
              timeout=120), "写平台代码路径")


def _dev_tool_src(pid: str, p: dict[str, Any], name: str, folder: str = "tools") -> Path:
    """开发区里 tools/<名>/（folder 写 subagents 就是 subagents/<名>/）的当前内容：在开发容器里打包（宿主不在开发区里跑命令），解到 joyzhang14 的目录；拒收链接与设备文件。"""
    data = _ok(_drun(container(pid, p, ["tar", "-C", f"/repo/{folder}/{name}", "-c", "."]), timeout=120), f"取开发区的 {folder}/{name}")
    dest = _trial_root(pid) / "src" / name if folder == "tools" else _trial_root(pid) / "src" / folder / name
    shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        if bad := [m.name for m in tar.getmembers() if not (m.isfile() or m.isdir())]:
            raise _err(f"{folder}/{name}/ 里有链接或特殊文件：{bad[:5]}", "删掉它们（git rm），只放普通文件", "new-tool" if folder == "tools" else "new-subagent")
        tar.extractall(dest, filter="data")
    return dest


TRIAL_KEEP_S = 6 * 3600                # builder_try_tool 留着的工具进程最多算在跑多久（同子代理 builder 的单次上限）：登记的 until 按它写，stbcc-builder 崩了容器也不会一直算在跑
_TRIALS: dict[str, dict[str, "_Trial"]] = {}   # 运行 → 工具 → builder_try_tool 留着的试跑进程（这一轮 builder 调用里接着用）


class _Trial:
    """一个按上线方式（sparky 用户、Landlock、计划的联网、读得到 .env 的凭证、运行信息）起的预演目录里的工具进程：
    start 起好、列出函数，call 在同一进程里调函数（打开的文档等状态留着），close 停进程。
    在跑期间在运行目录写登记 trial.json（同正式运行的 run.json，停了改成 ended）：工具以 STB_RUN_REF = builder-trial/<计划> 请 stbcc-docker 起容器时，
    它认这个登记、按计划里批过的 docker 声明起（docker_service.trial_live）；停了就不能再借这个运行起容器，在跑的也停。
    MCP 会话要在同一个协程里进出，所以整段放在一个后台任务里，调用经队列交进去。"""

    def __init__(self, pid: str, p: dict[str, Any], spec: Any, keep: float, digest: str = ""):
        self.pid, self.spec, self.digest = pid, spec, digest
        self.t = next(x for x in p["plan"]["tools"] if x["name"] == spec.name)
        root = _trial_root(pid)
        self.run_dir, self.work, self.errf = root / "run" / spec.name, root / "work", root / f"{spec.name}.stderr.log"
        self.reg = self.run_dir / "trial.json"     # docker_service.trial_file；data/hub 对 sparky 只读，只有平台写得了
        self.trial = {"status": "running", "tool": spec.name, "docker": self.t.get("docker"), "network": self.t["network"], "until": time.time() + keep}
        self.functions: list[str] = []
        self.q: asyncio.Queue[Any] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None
        self.cur: asyncio.Future[Any] | None = None

    async def start(self) -> None:
        """起工具、列出函数；缺凭证抛 env.EnvError，起不来抛原来的异常（进程已停）。"""
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        name, run_dir = self.spec.name, self.run_dir
        (run_dir / "signal").mkdir(parents=True, exist_ok=True)
        self.work.mkdir(exist_ok=True)
        env_need = {"STB_RUN_ID": "trial", "STB_RUN_REF": f"builder-trial/{self.pid}", "STB_PAUSE_FILE": str(run_dir / "signal" / "pause"),   # 运行信息同网关（registry.RUN_ENV）
                    "STB_WORK_ROOT": str(self.work), "STB_RUN_DIR": str(run_dir), "STB_RUN_IN": str(run_dir / "in"),
                    **({"STB_TOOL_RUN_DIR": str(run_dir / name)} if name not in launch.RUN_DIR_OWN else {})}
        if name not in launch.RUN_DIR_OWN:
            launch.grant(run_dir / name)
        cmd, envs = agent_command(self.spec, self.work, "trial", env_need, stage=_stage_home(self.pid), network=self.t["network"])
        ready: asyncio.Future[list[str]] = asyncio.get_running_loop().create_future()

        async def serve() -> None:
            gone: BaseException = RuntimeError("工具进程已退出")
            try:
                with self.errf.open("w") as log:
                    async with stdio_client(StdioServerParameters(command=cmd[0], args=cmd[1:], env=envs), errlog=log) as (r, w):
                        async with ClientSession(r, w) as s:
                            await s.initialize()
                            ready.set_result([x.name for x in (await s.list_tools()).tools])
                            while (item := await self.q.get()) is not None:
                                fn, args, fut = item
                                self.cur = fut
                                res = await s.call_tool(fn, args)
                                if not fut.done():
                                    fut.set_result(res)
            except Exception as e:  # noqa: BLE001 — 起不来、协议断了：交给在等的那一方
                gone = e
            finally:
                waiting = [ready, self.cur]
                while not self.q.empty():
                    if item := self.q.get_nowait():
                        waiting.append(item[2])
                for f in waiting:
                    if f is not None and not f.done():
                        f.set_exception(gone)

        _write_json(self.reg, self.trial)
        self.task = asyncio.create_task(serve())
        try:
            self.functions = await ready
        except BaseException:
            await self.close(0)
            raise

    def alive(self) -> bool:
        return self.task is not None and not self.task.done()

    async def call(self, fn: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self.alive():
            raise RuntimeError("工具进程已退出")
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        await self.q.put((fn, args, fut))
        res = await fut
        return {"ok": not res.is_error, "text": "".join(getattr(c, "text", "") for c in res.content)}

    async def close(self, wait: float = 15) -> None:
        """停进程（先让会话正常退出，wait 秒还没退就取消），登记改成 ended。"""
        if self.task is not None and not self.task.done():
            self.q.put_nowait(None)
            try:
                await asyncio.wait_for(asyncio.shield(self.task), wait)
            except asyncio.TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
        _write_json(self.reg, {**self.trial, "status": "ended"})

    def stderr_size(self) -> int:
        try:
            return self.errf.stat().st_size
        except OSError:
            return 0

    def stderr(self, since: int = 0) -> str:
        """since 字节之后工具打的日志与 traceback 的尾部。"""
        try:
            with self.errf.open("rb") as f:
                f.seek(since)
                return f.read().decode(errors="replace")[-3000:]
        except OSError:
            return ""


async def _drive(tr: _Trial, steps: list[tuple[str, dict[str, Any]]], timeout: float, why: str, fresh: bool, keep: bool) -> dict[str, Any]:
    """在 tr 里（fresh：先起它）按顺序调 steps，有一步失败就停；整段限时 timeout 秒（why 是上限的来由）。出了异常（超时、协议断了）或 keep=False 就停进程。
    返回 {started, functions, ok, text, stderr}，多步时 ok 是每步都成，每步的 function、ok、text 在 steps 里；stderr 是这次期间工具打的日志与 traceback 的尾部。"""
    out: dict[str, Any] = {"started": not fresh, **({} if fresh else {"functions": tr.functions})}
    done: list[dict[str, Any]] = []
    since = 0 if fresh else tr.stderr_size()

    async def go() -> None:
        if fresh:
            await tr.start()
            out.update(started=True, functions=tr.functions)
        for fn, args in steps:
            done.append({"function": fn, **await tr.call(fn, args)})
            if not done[-1]["ok"]:
                break

    try:
        await asyncio.wait_for(go(), timeout)
    except env.EnvError as e:
        return {"started": False, "error": f"{e}（改法：用 builder_request_credential 让 Joy 填）", "stderr": ""}
    except asyncio.TimeoutError:
        what = "、".join((["起工具"] if fresh else []) + [f"调 {fn}" for fn, _ in steps])
        out["error"] = (f"试跑超时：整次试跑（{what}）上限 {timeout:.0f} 秒（{why}），到时还没完，工具进程已停。"
                        "要加载模型、跑得久的，计划的 docker.timeout 要写够；没起容器也超时就看 stderr 卡在哪")
    except Exception as e:  # noqa: BLE001 — 起不来、协议断了都如实返回
        out["error"] = f"{type(e).__name__}: {str(e)[:500]}"
    if "error" in out or not keep:
        await tr.close(0 if "error" in out else 15)
    if len(steps) > 1:
        out.update(ok=len(done) == len(steps) and all(d["ok"] for d in done), steps=done)
    elif done:
        out.update(ok=done[0]["ok"], text=done[0]["text"])
    out["stderr"] = tr.stderr(since)
    return out


async def run_trial(pid: str, p: dict[str, Any], spec: Any, call: str | None = None, args: dict[str, Any] | None = None,
                    timeout: float | None = None) -> dict[str, Any]:
    """起一次预演目录里的工具（_Trial）：能不能起来、有哪些函数；call 给了就再调一次；用完就停（合入前试跑用）。
    返回 {started, functions, ok, text, stderr}；stderr 是工具自己打的日志与 traceback 的尾部。
    timeout 不给就按计划里这个工具算（trial_timeout）；超时报错写明上限与来由。"""
    t = next(x for x in p["plan"]["tools"] if x["name"] == spec.name)
    timeout, why = (timeout, f"调用方给的 {timeout:.0f} 秒") if timeout is not None else trial_timeout(t)
    return await _drive(_Trial(pid, p, spec, timeout), [(call, args or {})] if call else [], timeout, why, fresh=True, keep=False)


def trial_failure(name: str, r: dict[str, Any], what: str) -> str | None:
    """试跑结果 → 失败说明（没失败返回 None）。"""
    if not r.get("started"):
        why = f"起不来：{r.get('error', '')}"
    elif r.get("ok") is False or r.get("error"):
        why = f"{what}失败：{(r.get('text') or r.get('error') or '')[:800]}"
    else:
        return None
    return (f"工具 {name} 在真实运行环境（sparky 用户 + 计划的联网与凭证）里{why}" + (f"\n工具的 stderr 尾部：\n{r['stderr'][-1500:]}" if r.get("stderr") else "")
            + f"\n改法：看上面的原始报错（traceback 的最后几行指明了文件和行）；PermissionError 多半是读写了 sparky 没有权限的路径（可写的只有 STB_STATE_DIR、本次运行的 media 目录等平台开好的目录），"
              f"ModuleNotFoundError 是依赖没写进计划 deps（与 requirements.txt），凭证错就用 builder_request_credential 让 Joy 重填；"
              f"改完用 builder_try_tool(\"{name}\", …) 先试，再 builder_verify\n规范：builder_guide(\"new-tool\")")


def pre_merge_trial(pid: str, p: dict[str, Any], check: Path) -> str:
    """合入前：check 仓库里每个计划工具装进预演目录，按真实运行方式起一次；要凭证的再调 <名>_check。任何一个不过就抛错、不合入。"""
    from control_center import registry

    done = []
    for t in p["plan"]["tools"]:
        src = check / "tools" / t["name"]
        with _TRIAL_LOCK:
            _install_tool(pid, t["name"], t["deps"], src, _stage_home(pid), fresh_venv=False)
            spec = registry.parse_tool_yaml(src / "tool.yaml", check)
            r = asyncio.run(run_trial(pid, p, spec, f"{t['name']}_check" if tool_credentials(src) else None))
        if why := trial_failure(t["name"], r, "_check "):      # 不自动重试：报错原样交给模型，由它判断是网络抖动（再交一次）还是要改
            raise BuilderError(why)
        done.append(t["name"])
    return "、".join(done) or "（没有新工具）"


def _trial_calls(name: str, function: str, args: dict[str, Any] | None, steps: Any) -> list[tuple[str, dict[str, Any]]]:
    """builder_try_tool 要调的：function + args 一步，或 steps（[{function, args}, …]）几步；函数都要以 <名>_ 开头。"""
    if steps is not None and function:
        raise _err("function 和 steps 只写一个", "调一个函数写 function（和 args）；连着调几个不写 function，写 steps：[{\"function\": …, \"args\": {…}}, …]")
    raw = steps if steps is not None else [{"function": function, "args": args or {}}]
    if not isinstance(raw, list) or not raw or not all(isinstance(x, dict) and isinstance(x.get("function"), str) and isinstance(x.get("args") or {}, dict) for x in raw):
        raise _err("steps 格式不对", f"steps 写成 [{{\"function\": \"{name}_…\", \"args\": {{…}}}}, …]，至少一步")
    for x in raw:
        if not x["function"].startswith(f"{name}_"):
            raise _err(f"函数 {x['function']} 不以 {name}_ 开头", f"function 写 {name}_<动作>（builder 的工具函数都带工具名前缀）", "new-tool")
    return [(x["function"], x.get("args") or {}) for x in raw]


def _tree_digest(d: Path) -> str:
    """目录里文件的路径与内容的指纹（不算 __pycache__）：留着的试跑进程按它判断代码改没改。"""
    h = hashlib.sha256()
    for f in sorted(x for x in d.rglob("*") if x.is_file() and "__pycache__" not in x.parts):
        h.update(str(f.relative_to(d)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()


async def try_tool(name: str, function: str = "", args: dict[str, Any] | None = None, steps: Any = None, restart: bool = False,
                   run: str = "") -> dict[str, Any]:
    """builder_try_tool：开发区里 tools/<名>/ 的当前代码装进预演目录，按上线后的方式起它，按顺序调 function（或 steps 里的几步）。
    工具进程留着（_TRIALS，按发请求的运行 run 分）：下次接着用，状态还在；代码改了、restart、进程已退出就重起（结果带 restarted）。
    这一轮 builder 调用结束（子代理 builder 的 post_call hook）、运行不在跑、builder_verify / builder_submit 之前由 end_trials 关掉。不可信工具只回成败与字数，不回内容。"""
    from control_center import registry

    p = _ready()
    t = next((x for x in p["plan"]["tools"] if x["name"] == name), None)
    if t is None:
        raise _err(f"{name} 不是本计划的工具（只试本计划 tools 里的新工具）", "builder_try_tool 只试本计划 tools 里的工具；改已有工具的用 builder_verify（部署检查同样会试）")
    calls = _trial_calls(name, function, args, steps)
    await asyncio.to_thread(_TRIAL_LOCK.acquire)        # 并发的试跑（同一轮里并行调用）排队：取代码、装预演目录、试跑整段一次一个
    try:
        src = await asyncio.to_thread(_dev_tool_src, p["id"], p, name)
        try:
            spec = registry.parse_tool_yaml(src / "tool.yaml", src.parent.parent)
        except (ValueError, OSError) as e:
            raise _err(f"tools/{name}/tool.yaml 不合格：{e}", "照报错改 tool.yaml 的那一项", "new-tool") from None
        digest = await asyncio.to_thread(_tree_digest, src)
        tr, note = _TRIALS.get(run, {}).pop(name, None), ""
        if tr is not None:
            note = ("按 restart 重起" if restart else "计划换了" if tr.pid != p["id"] else "代码改了" if tr.digest != digest
                    else "上次的进程已经退出" if not tr.alive() else "")
            if note:
                await tr.close()
        if tr is None or note:
            await asyncio.to_thread(_install_tool, p["id"], name, t["deps"], src, _stage_home(p["id"]), False)
            tr = _Trial(p["id"], p, spec, TRIAL_KEEP_S, digest)
        timeout, why = trial_timeout(t)
        r = await _drive(tr, calls, timeout, why, fresh=not tr.alive(), keep=True)
        if tr.alive():
            _TRIALS.setdefault(run, {})[name] = tr
        if note:
            r["restarted"] = f"重新起了工具进程（{note}），之前的状态没了"
    finally:
        _TRIAL_LOCK.release()
    for d in [r, *r.get("steps", [])]:
        if spec.usable_by == "subagent" and "text" in d:
            d["text"] = f"（读外部内容的工具：内容不给你，只说长度 {len(d['text'])} 字；要看内容走隔离读者）"
        elif "text" in d:
            d["text"] = d["text"][:3000]
    return r


async def end_trials(run: str) -> str:
    """关掉运行 run 里 builder_try_tool 留着的试跑进程（这一轮 builder 调用结束、builder_verify / builder_submit 之前）。返回关了哪些工具。"""
    await asyncio.to_thread(_TRIAL_LOCK.acquire)
    try:
        gone = _TRIALS.pop(run, {})
        for tr in gone.values():
            await tr.close()
    finally:
        _TRIAL_LOCK.release()
    return "、".join(gone) or "（没有留着的）"


async def prune_trials(live: Any) -> None:
    """live(run) 为假（运行完成、失败、让出、暂停、被停）的，关掉它留着的试跑进程：这一轮 builder 调用没走到 post_call hook 时的兜底。"""
    for run in [r for r in _TRIALS if not live(r)]:
        await end_trials(run)


def trial_subagents(p: dict[str, Any]) -> list[str]:
    """builder_try_subagent 能试的子代理：本计划 subagents 新建的与 change_subagents 改的。"""
    return [s["name"] for s in p["plan"].get("subagents") or []] + [c["name"] for c in p["plan"].get("change_subagents") or []]


def stage_subagent_dir(pid: str, name: str) -> Path:
    """子代理的预演副本：/home/sparky/stage/<计划>/subagents/<名>/（归 sparky；平台用户能读：stage/ 对别人可读，/home/sparky 能进入）。"""
    return _stage_home(pid) / "subagents" / name


def trial_subagent_def(p: dict[str, Any], name: str) -> subagents.SubagentDef:
    """试跑用的子代理定义：yaml 用计划批准的内容（_subagent_config，不认开发区里改过的），文件（server.py、hooks、guide.md、output.py）用预演副本；
    按子代理规则完整校验。工具只认已登记的（计划的新工具还没上线，子代理里试不了）；隔离、is_editable: false 的不试（同 _parse_subagent）。"""
    try:
        d = subagents.parse_def(name, _subagent_config(p["plan"], name), folder=stage_subagent_dir(p["id"], name))
    except subagents.SubagentError as e:
        raise _err(f"子代理 {name} 试跑不了：{e}", "按报错改（server.py、hooks 的文件要在 subagents/<名>/ 里并已 git commit）；tools 里有本计划的新工具的，要合入后才能在子代理里真跑，"
                   "先用 builder_try_tool 试那个工具", "new-subagent") from None
    if d.isolated or not d.is_editable:
        raise _err(f"子代理 {name} 写了 isolated / is_editable: false", "去掉：隔离子代理只由 Joy 建、改", "new-subagent")
    return d


def stage_subagent(name: str) -> dict[str, Any]:
    """builder_try_subagent 的第一步（stbcc-builder 做）：只认本计划新建 / 改的子代理；开发区 subagents/<名>/ 的当前内容（拒链接）的 yaml 要和计划批准的一致，
    以 sparky 拷进预演目录 stage_subagent_dir，再按试跑的规则校验（trial_subagent_def）；档位要已在 models.yaml 里（计划新加的档位合入后才有）。
    真跑由网关接着做（gateway._try_subagent），这里不起任何东西。"""
    p = _ready()
    if name not in trial_subagents(p):
        raise _err(f"{name} 不是本计划新建或改的子代理", f"builder_try_subagent 只试本计划 subagents / change_subagents 里的：{', '.join(trial_subagents(p)) or '（本计划没有）'}", "new-subagent")
    with _TRIAL_LOCK:
        return _stage_subagent(p, name)


def _stage_subagent(p: dict[str, Any], name: str) -> dict[str, Any]:
    from control_center.models import load_models

    src = _dev_tool_src(p["id"], p, name, "subagents")
    rel = f"subagents/{name}/{subagents.YAML_FILE}"
    got = yaml.safe_load((src / subagents.YAML_FILE).read_text()) if (src / subagents.YAML_FILE).is_file() else None
    if got != (want := _subagent_config(p["plan"], name)):
        diff = sorted(k for k in set(got or {}) | set(want) if (got or {}).get(k) != want.get(k))
        raise _err(f"开发区的 {rel} 和计划批准的不一致：{diff} 不对", "照计划写（新建：config 原样；改：只改 fields 里的字段），git commit 后再试", "new-subagent")
    code = str(stage_subagent_dir(p["id"], name))
    sudo("rm", "-rf", code)
    sudo("mkdir", "-p", code)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(src, arcname=".")
    sudo("tar", "-x", "-C", code, "-f", "-", input=buf.getvalue())
    d = trial_subagent_def(p, name)
    try:
        tier = subagents.profile_name(d, load_models(), "") or "主模型（不换）"
    except subagents.SubagentError:
        raise _err(f"子代理 {name} 的档位（{d.runtime} + {d.model}）还不在 models.yaml 里（本计划新加的档位合入后才写进去）",
                   "这次只能靠单测；合入后再让主模型正式调用它试", "model-tier") from None
    return {"plan": p["id"], "subagent": name, "kind": d.kind, "model": tier, "folder": code}


async def check(name: str) -> dict[str, Any]:
    """builder_check：重新验证一个已部署工具的凭证；过了而且部署时因为没过而没加进任务，就现在按 add_to 加。"""
    hits = [p for f in (_dir() / "plans").glob("*.json") if (p := _read_json(f, {})).get("status") in ("deployed", "failed")
            and any(t["name"] == name for t in p["plan"]["tools"])]
    if not hits:
        raise BuilderError(f"没有部署过的计划里有工具 {name}")
    p = max(hits, key=lambda p: p.get("created", 0))
    ok, out = await check_tool(p, name)
    r: dict[str, Any] = {"tool": name, "ok": ok, "output": out}
    if ok and name in (p.get("unchecked") or []):
        r["add_to"] = add_to_tasks(p, {name})
        p["unchecked"] = [x for x in p["unchecked"] if x != name]
        save_plan(p)
    return r


# ---------- 外部命令 ----------
def _run(argv: list[str], input: bytes | None = None, timeout: float = 600, cwd: str | Path | None = "/",
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    """所有外部命令的唯一出口（测试替换它）。env 不给 = 继承本进程的环境。"""
    if input is None:                     # 没有输入就给空 stdin：MCP 服务器的 stdin 是协议流，不能让子进程（docker -i）读走
        input = b""
    return subprocess.run(argv, input=input, capture_output=True, timeout=timeout, cwd=cwd, env=env)


def _drun(req: dict[str, Any], input: bytes | None = None, timeout: float = 600) -> subprocess.CompletedProcess[bytes]:
    """开发容器的唯一出口（测试替换它）：请 stbcc-docker 代起（平台自己的容器，不看 tool.yaml，资源按上限；连接一断容器就停）。
    req 是 docker_client.run 的参数（image、argv、mounts…）。服务不在、被拒抛 BuilderError。"""
    from control_center import docker_client

    try:
        return docker_client.run(**req, input=input or b"", timeout=timeout)
    except docker_client.DockerError as e:
        raise BuilderError(f"开发容器没起来：{e}") from None


def _dbuild(tool: str) -> subprocess.CompletedProcess[bytes]:
    """请 stbcc-docker 用工具目录的 Dockerfile 建镜像 stb-<工具名>（测试替换它）。"""
    from control_center import docker_client

    try:
        r = docker_client.build(tool, timeout=3600)
    except docker_client.DockerError as e:
        raise BuilderError(f"建镜像没成：{e}") from None
    return subprocess.CompletedProcess(r.args, r.returncode, r.stdout, r.stdout[-4000:])


def _ok(r: subprocess.CompletedProcess[bytes], what: str) -> bytes:
    if r.returncode != 0:
        raise BuilderError(f"{what} 失败（退出码 {r.returncode}）：{r.stderr.decode(errors='replace')[-800:]}")
    return r.stdout


def sudo(*argv: str, input: bytes | None = None, timeout: float = 600) -> bytes:
    """以 sparky 身份跑（-H：HOME 换成 sparky 的，git 不去读 joyzhang14 的配置）。只用在平台自己生成的输入上，不在开发区里跑。"""
    return _ok(_run(["sudo", "-n", "-u", SPARKY, "-H", "--", *argv], input=input, timeout=timeout), f"sudo -u sparky {argv[0]}")


def git(*argv: str, cwd: Path | None = None, timeout: float = 300) -> str:
    """cwd 不给就是仓库（调用时才取 REPO，测试换得掉）。"""
    return _ok(_run(["git", *argv], timeout=timeout, cwd=cwd or REPO), f"git {argv[0]}").decode(errors="replace")


def spawn(op: str, pid: str) -> None:
    """worker 放进独立的 systemd 用户单元：不在 stbcc-hub / stbcc-builder / stbcc-runner 等服务的 cgroup 里，重启这些服务不会带走它。"""
    stb = Path(sys.executable).parent / "stb"
    _ok(_run(["systemd-run", "--user", "--collect", "--quiet", f"--unit=stb-builder-{op}-{pid}-{secrets.token_hex(2)}",   # UMask 0022：合入的代码组只读（仓库归 sparkle 组）
              f"--working-directory={REPO}", "-p", "UMask=0022", str(stb), "builder", "worker", op, pid], timeout=30), "systemd-run")


def dev_dir(pid: str) -> Path:
    return SPARKY_HOME / "dev" / pid


def _repo_paths(base: str) -> list[str]:
    """仓库 venv 里指向仓库的可编辑安装路径（仓库根、services/owa-research…），仓库根换成 base。"""
    paths = [base]
    root = str(VENV.parent)
    for pth in sorted(VENV.glob("lib/python3*/site-packages/*.pth")):
        for line in pth.read_text(errors="replace").splitlines():
            if line.startswith(root + "/"):
                paths.append(base + line[len(root):])
    return paths


def _pythonpath(plan: dict[str, Any]) -> str:
    """容器里的 PYTHONPATH：/repo + 仓库 venv 里指向仓库的可编辑安装路径（换成 /repo 下）+ 计划依赖的 /deps/<名>、/deps/tasks/<名>（dep_owners）。"""
    return ":".join(_repo_paths("/repo") + [_deps_target(kind, name) for kind, name, _ in dep_owners(plan["plan"])])


def container(pid: str, plan: dict[str, Any], argv: list[str], *, cwd: str = "", deps_rw: bool = False,
              src: Path | None = None, memory: str = "4g") -> dict[str, Any]:
    """stb-dev 容器（交 stbcc-docker 代起，_drun）：断网、uid 1001、只读根（服务统一加）。/repo = 开发区克隆（读写）；
    src 给了就改成内存盘，/src 只读挂 src（部署测试用）。"""
    d = dev_dir(pid)
    tmpfs = [{"path": "/tmp", "size": "1g", "exec": True}, {"path": "/home/dev", "size": "256m", "exec": True}]
    mounts = [{"host": "/etc/timezone", "container": "/etc/timezone", "mode": "ro"}, {"host": "/etc/localtime", "container": "/etc/localtime", "mode": "ro"},
              {"host": str(VENV), "container": "/opt/stb-venv", "mode": "ro"}]
    if src:
        tmpfs.append({"path": "/repo", "size": "2g", "exec": True})
        mounts.append({"host": str(src), "container": "/src", "mode": "ro"})
    else:
        mounts.append({"host": str(d / "repo"), "container": "/repo", "mode": "rw"})
    mounts += [{"host": str(d / "wheelhouse"), "container": "/wheelhouse", "mode": "ro"}, {"host": str(d / "deps"), "container": "/deps", "mode": "rw" if deps_rw else "ro"}]
    return {"image": DEV_IMAGE, "argv": list(argv), "user": DEV_USER, "init": True, "memory": memory, "cpus": 4, "pids": 512, "tmp": "4g", "tmpfs": tmpfs,
            "env": {"HOME": "/home/dev", "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/opt/stb-venv/bin:/usr/bin:/bin",
                    "PYTHONPATH": _pythonpath(plan), "GIT_AUTHOR_NAME": "sparky-builder", "GIT_AUTHOR_EMAIL": "builder@sparky",
                    "GIT_COMMITTER_NAME": "sparky-builder", "GIT_COMMITTER_EMAIL": "builder@sparky"},
            "mounts": mounts, "workdir": "/repo" + (f"/{cwd}" if cwd else "")}


def _ready() -> dict[str, Any]:
    p = active_plan()
    if p is None or p["status"] != "ready":
        raise _err("没有已批准、开发区已就绪的计划" + (f"（当前计划 {p['id']} 是 {p['status']}）" if p else ""),
                   "开发计划由主模型调用 builder 时提案、Joy 批准后才有；deploying / merged 时等部署结果，failed / deployed 的计划要改就让主模型提新计划")
    return p


def _rel(path: str) -> str:
    rel = str(path).strip()
    if not rel or rel.startswith("/") or any(x in ("..", ".git") for x in Path(rel).parts):
        raise _err(f"路径 {path!r} 不行", "写仓库内的相对路径（如 tools/<工具名>/server.py），不能以 / 开头、不能有 .. 或 .git")
    return str(Path(rel))


def _tail(b: bytes) -> str:
    s = b.decode(errors="replace")
    return s if len(s) <= OUT_CHARS else f"…[前面省略 {len(s) - OUT_CHARS} 字符]\n" + s[-OUT_CHARS:]


def dev_run(argv: list[str], cwd: str = "", timeout: int = 600) -> dict[str, Any]:
    p = _ready()
    if not argv or not all(isinstance(a, str) for a in argv):
        raise BuilderError("argv 要是非空的字符串列表，如 [\"python\", \"-m\", \"pytest\", \"tests/stb_platform/test_x.py\", \"-q\"]")
    t = max(1, min(int(timeout), 1800))
    try:
        r = _drun(container(p["id"], p, list(argv), cwd=_rel(cwd) if cwd else ""), timeout=t)
    except subprocess.TimeoutExpired:                 # 连接一断，stbcc-docker 就停掉容器
        return {"exit_code": None, "timed_out": True, "note": f"超过 {t} 秒，容器已停掉"}
    return {"exit_code": r.returncode, "stdout": _tail(r.stdout), "stderr": _tail(r.stderr)}


WRITE_SRC = "import pathlib,sys\np=pathlib.Path(sys.argv[1])\np.parent.mkdir(parents=True,exist_ok=True)\np.write_bytes(sys.stdin.buffer.read())\n"


def write_file(path: str, content: str) -> str:
    p = _ready()
    rel = _rel(path)
    _ok(_drun(container(p["id"], p, ["python3", "-c", WRITE_SRC, rel]), input=content.encode(), timeout=120), "写文件")
    return f"已写入 {rel}（{len(content)} 字符）"


# ---------- 计划里的工具的试跑与凭证检查（启动方式同 control_center/launch.py：sparky 用户 + Landlock） ----------
def agent_state(name: str) -> Path:
    """计划里的工具上线后的状态目录 data/tool_state/<名>/（STB_STATE_DIR；builder 起它查凭证时也当 HOME，tmp/ 是 TMPDIR）：同别的工具（launch.tool_state：
    没有就现建；整个 tool_state/ 对 sparky 读写）。"""
    d = launch.tool_state(name)
    (d / "tmp").mkdir(exist_ok=True)                 # 继承目录的组默认可写：sparky 可写
    return d


def grant_media(work_dir: Path, run_id: str) -> Path:
    """本次运行的 media 目录（工作目录 media/<运行 id>/）建好并共享给 sparkle 组读写（launch.grant）：sparky 写出的文件网关用户（joyzhang14）也能读（reply 发附件）。"""
    d = work_dir / "media" / run_id
    launch.grant(d)
    return d


def agent_command(spec: Any, work_dir: Path, run_id: str, need: dict[str, str] | None = None,
                  stage: Path | None = None, network: Any = None, run_dir: Path | None = None) -> tuple[list[str], dict[str, str]]:
    """builder 起计划里的工具（合入前试跑 run_trial、部署后凭证检查 check_tool）：sudo 成 sparky，环境只有 state 目录、工作目录与运行信息（need：registry.RUN_ENV 那些）；
    启动器源码来自仓库（sparky 改不了），按联网上 Landlock（同正式运行 launch.net_cfg：false 禁 TCP，端口列表只放行这些端口）再跑服务器；文件看 sparky 的文件权限
    （平台仓库整个可读，含 .env；服务器 `from control_center.credential import credential`），可写的是自己的 state、
    本次运行的 media 目录和本工具的运行目录（平台固定准备的，同别的工具：launch.prepare）。联网：network 参数（试跑时按计划）> tool.yaml 的 network。
    上线后：代码就是仓库的 tools/<名>/，有 requirements.txt 的 venv 在 data/runtime/venvs/tools/<名>（没有的用平台的 venv），状态在 data/tool_state/<名>（agent_state）；
    stage（合入前试跑的预演目录）给了就用它下面的 tools/ venvs/ state/。沙箱里外同一个路径（STB_WORK_ROOT 是工作目录）。"""
    if stage is not None:
        code, venv, state = (stage / d / spec.name for d in ("tools", "venvs", "state"))
        server = code / spec.server.name
        media = grant_media(work_dir, run_id)
    else:
        server, venv, state = spec.server, venv_dir(spec.name), agent_state(spec.name)
        code = server.parent
        media = launch.prepare(spec.name, launch.RunCtx(work_dir, run_dir, run_id))
    envs = {"STB_STATE_DIR": str(state), "HOME": str(state), "TMPDIR": str(state / "tmp"),
            "STB_WORK_ROOT": str(work_dir), "STB_MEDIA_DIR": str(media)}
    envs.update(need or {})
    net = network if network is not None else spec.network
    cfg = {**launch.net_cfg(net), "code": str(code), "server": str(server)}
    python = str(venv / "bin" / "python") if spec.deps else sys.executable   # 没有 requirements.txt 的用平台的 venv（同 registry.python_of）
    return launch.sparky_command(python, cfg, envs, spec.name), envs


# ---------- deploy ----------
def _log(pid: str, msg: str) -> None:
    f = _dir() / pid / "deploy.log"
    f.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    with f.open("a") as fh:
        fh.write(time.strftime("%H:%M:%S ") + msg + "\n")


def _locked_owner(path: str) -> str | None:
    """路径在 is_editable: false 的工具 / 子代理文件夹里：返回「工具 <名>」/「子代理 <名>」，否则 None。"""
    from control_center.registry import TOOLS

    parts = Path(path).parts
    if len(parts) >= 3 and parts[0] == "tools" and (t := TOOLS.get(parts[1])) is not None and not t.is_editable:
        return f"工具 {parts[1]}"
    if len(parts) >= 3 and parts[0] == "subagents" and subagents.is_folder(parts[1]):
        try:
            if (yaml.safe_load(subagents.def_path(parts[1]).read_text()) or {}).get("is_editable") is False:
                return f"子代理 {parts[1]}"
        except (OSError, yaml.YAMLError):
            return None
    return None


def _allowed(path: str, plan: dict[str, Any]) -> bool:
    """计划范围：新工具 tools/<名>/、新任务与改的已有任务的文件夹 tasks/<名>/（task.yaml、listen.py、命令脚本等；listen.py 另按计划的 listen_py 核对）、改的已有工具的文件夹 tools/<名>/、
    新建 / 改的子代理的文件夹 subagents/<名>/ 里的文件（具体哪些文件由 _check_subagents 按计划核对）、对应的 tests/stb_platform/test_<名>.py。"""
    parts = Path(path).parts
    tools = {t["name"] for t in plan["plan"]["tools"]}
    tasks = {t["name"] for t in plan["plan"]["tasks"]}
    changed_tools = {c["name"] for c in plan["plan"].get("change_tools") or []}
    changed_tasks = {c["name"] for c in changes_with_retire(plan["plan"])}
    agents = {s["name"] for s in [*(plan["plan"].get("subagents") or []), *(plan["plan"].get("change_subagents") or [])]}
    tests = {f"test_{n.replace('-', '_')}.py" for n in tools | tasks | changed_tools | changed_tasks | agents}
    return ((len(parts) >= 3 and parts[0] == "tools" and parts[1] in tools)
            or (len(parts) == 3 and parts[0] == "subagents" and parts[1] in agents)
            or (len(parts) >= 3 and parts[0] == "tasks" and parts[1] in tasks | changed_tasks)
            or (len(parts) >= 3 and parts[0] == "tools" and parts[1] in changed_tools)
            or any(path in v for v in retire_files(plan["plan"]).values())
            or (len(parts) == 3 and parts[:2] == ("tests", "stb_platform") and parts[2] in tests))


def with_tool(text: str, name: str, opts: dict[str, Any]) -> str:
    """任务 yaml 原文的 tools 列表末尾加一项（只插行，注释和格式不动）；已经有这个工具就原样返回。"""
    raw = yaml.safe_load(text) or {}
    if any((i if isinstance(i, str) else next(iter(i), None)) == name for i in raw.get("tools") or [] if isinstance(i, (str, dict))):
        return text
    item = yaml.safe_dump([{name: opts} if opts else name], allow_unicode=True, sort_keys=False).splitlines()
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if re.match(r"tools:\s*(#.*)?$", l)), None)
    if start is None:                                    # 没有 tools 键：文件末尾补一段
        return text.rstrip("\n") + "\ntools:\n" + "".join(f"  {l}\n" for l in item)
    block = []                                           # tools 块：到下一个顶格的键为止
    for i in range(start + 1, len(lines)):
        if lines[i].strip() and not lines[i][0].isspace() and not lines[i].startswith("#"):
            break
        block.append(i)
    items = [i for i in block if lines[i].lstrip().startswith("- ")]
    indent = lines[items[0]][: len(lines[items[0]]) - len(lines[items[0]].lstrip())] if items else "  "
    end = max([i for i in block if lines[i].strip() and not lines[i].lstrip().startswith("#")], default=start)
    return "\n".join(lines[: end + 1] + [indent + l for l in item] + lines[end + 1:]) + ("\n" if text.endswith("\n") else "")


def _add_to_check(root: Path, plan: dict[str, Any], specs: dict[str, Any]) -> None:
    """add_to：在 check 仓库里把新工具加进目标任务的 yaml，用 load_task 完整校验（工具项的未知键…），校验完恢复原文，不进合入提交。"""
    from control_center import registry
    from control_center import paths
    from control_center.task import TaskError, load_task

    for t in plan["plan"]["tools"]:
        for k, opts in (t.get("add_to") or {}).items():
            f = root / "tasks" / paths.task_file(k).relative_to(paths.TASKS)
            text = f.read_text()
            saved = dict(registry.TOOLS)
            registry.TOOLS.update(specs)
            try:
                f.write_text(with_tool(text, t["name"], opts))
                load_task(f, plan_models(plan), refresh=False)
            except TaskError as e:
                raise _err(f"工具 {t['name']} 按计划的 add_to 加进任务 {k} 后校验失败：{e}",
                           f"add_to 的工具项写在计划里（一般是 {{}}，只能写 guide 这个网关项）：按报错改计划，"
                           "改计划要重新提计划（Joy 再批一次）", "new-tool") from None
            finally:
                f.write_text(text)
                registry.TOOLS.clear()
                registry.TOOLS.update(saved)


def add_to_tasks(p: dict[str, Any], only: set[str] | None = None) -> list[str]:
    """部署成功（运行环境装好）后，按 add_to 把新工具（only：只加这几个）加进 main 上的任务 yaml 并提交；返回给 Joy 的说明行。失败的恢复原文、写明原因。"""
    from control_center.models import load_models
    from control_center import paths, registry
    from control_center.task import TaskError, load_task

    registry.refresh(REPO)                                # 新工具刚合入：按仓库重扫 tool.yaml 才认得它
    out = []
    for t in p["plan"]["tools"]:
        if only is not None and t["name"] not in only:
            continue
        for k, opts in (t.get("add_to") or {}).items():
            f = paths.task_file(k)
            rel = f"tasks/{f.relative_to(paths.TASKS)}"
            if git("status", "--porcelain", "--", rel).strip():
                out.append(f"⚠ 没把 {t['name']} 加给 {k}：{rel} 有未提交的改动，请手动加")
                continue
            text = f.read_text()
            new = with_tool(text, t["name"], opts)
            if new == text:
                out.append(f"{k} 已经有 {t['name']}")
                continue
            try:
                f.write_text(new)
                load_task(f, load_models(), refresh=False)
                git("add", "--", rel)
                git("-c", "user.name=stb-builder", "-c", "user.email=builder@sparky", "commit", "-q", "-m", f"feat(tasks): {k} 加上 builder 做的工具 {t['name']}", "--", rel)
                out.append(f"已把 {t['name']} 加给 {k}")
            except (TaskError, BuilderError) as e:
                f.write_text(text)
                out.append(f"⚠ 没把 {t['name']} 加给 {k}：{e}")
    return out


def tool_credentials(folder: Path) -> list[str]:
    """工具文件夹里的代码 credential("KEY") 读的键（静态扫，同 registry.credential_keys）：要凭证的工具要有 <名>_check，部署前 .env 里要有。"""
    from control_center.registry import credential_calls

    return sorted({k for f in folder.rglob("*.py") for k in credential_calls(f.read_text(errors="replace"))})


def unprefixed(spec: Any) -> list[str]:
    """builder 新做的工具里不以 <工具名>_ 开头的函数（builder 自己的规矩：免得和别的工具撞名；部署检查查）。"""
    from control_center.registry import _functions

    return [f for f in _functions(spec.server) if not f.startswith(f"{spec.name}_")]


def check_tree(root: Path, plan: dict[str, Any]) -> None:
    """check 仓库工作区（已是合入后的样子）里计划内的 tool.yaml、子代理文件夹与任务 yaml 是否与计划一致；再用 load_task 完整校验任务
    （子代理按 check 仓库里的 subagents/ 认）。每个错误写明文件、第几行、错在哪、应改成什么、对应规范。"""
    saved_dir = subagents.DIR
    subagents.DIR = root / "subagents"                   # 新建 / 改的子代理还没合入：校验任务时按 check 仓库里的认
    try:
        _check_tree(root, plan)
    finally:
        subagents.DIR = saved_dir


def _check_tree(root: Path, plan: dict[str, Any]) -> None:
    from control_center import registry
    from control_center.task import TaskError, load_task

    specs = {}
    for t in plan["plan"]["tools"]:
        rel = f"tools/{t['name']}/tool.yaml"
        f = root / rel
        if not f.is_file():
            raise _err(f"缺 {rel}", f"写 {rel}（name、description、network，和计划一致；服务器写同文件夹的 server.py；计划的 deps 写进同文件夹的 requirements.txt），git commit", "new-tool")
        try:
            spec = registry.parse_tool_yaml(f)
        except ValueError as e:
            raise _err(f"{rel} 解析不过：{e}", "按报错改那一项（字段说明：builder_guide(\"new-tool\")）", "new-tool") from None
        text = f.read_text()
        want = {"deps": sorted(t["deps"]),   # deps 比的是 requirements.txt；联网 tool.yaml 可不写（按计划）
                "docker": registry._docker(f, t["docker"]) if t.get("docker") else None, "network": t["network"] if spec.network is not None else None,
                "usable_by": "subagent" if t.get("untrusted") else "universal",   # 计划的 untrusted（读外部内容）= tool.yaml 的 usable_by: subagent
                "hooks": t.get("hooks")}                                         # hooks 和计划原文一模一样（计划没写 tool.yaml 也不写）
        got = {"deps": sorted(spec.deps), "docker": spec.docker, "network": spec.network,
               "usable_by": spec.usable_by, "hooks": (yaml.safe_load(text) or {}).get("hooks")}
        if diff := [k for k in want if got[k] != want[k]]:
            raise _err(f"{rel} 与计划不一致：" + "；".join((f"依赖（tools/{t['name']}/requirements.txt）" if k == "deps" else f"{k}（{_line(text, k)}）")
                                                         + f"是 {_show(got[k])}，计划是 {_show(want[k])}" for k in diff),
                       "把这几项改成和计划一样（计划已由 Joy 批准，不能在 tool.yaml 里改；真要改得重新提计划）", "new-tool")
        server = f"tools/{t['name']}/{spec.server.name}"
        if bad := unprefixed(spec):
            raise _err(f"{server} 的函数名 {bad} 要以 {spec.name}_ 开头", f"改成 {spec.name}_<动作>（免得和别的工具撞名）", "new-tool")
        creds = tool_credentials(f.parent)
        if creds and f"{spec.name}_check" not in registry._functions(spec.server):
            raise _err(f"工具 {t['name']} 要凭证，{server} 里没有 {spec.name}_check 函数",
                       f"加一个无参数的 {spec.name}_check：用凭证做一次最小的只读调用（如登录后返回账号名，不回显凭证），失败 raise ToolError(原因)", "credential-tool")
        if missing := [k for k in creds if not env.get(k)]:
            raise _err(f"工具 {t['name']} 要的凭证 .env 里还没有：{missing}", "用 builder_request_credential(key, note) 让 Joy 填（不用等他填完就能继续开发），填好再验", "credential-tool")
        specs[spec.name] = spec
    _check_subagents(root, plan, specs)
    for k in plan["plan"]["tasks"]:
        rel = task_rel(k)
        f = root / rel
        if not f.is_file():
            raise _err(f"缺 {rel}", f"写 {rel}（enabled: false，trigger / schedule / priority / runtime / model / tools / steps 和计划一致；agent 任务的说明写 system_prompt），git commit", "new-task")
        _listen_same(root, f"tasks/{k['name']}/listen.py", k.get("listen_py"), "listen", base=k.get("listen_py") is None)
        _req_same(root, f"tasks/{k['name']}", k.get("deps") or [], "workflow-task")
        text = f.read_text()
        if bad := set(re.findall(r"\$\{([^}]*)\}", text)) - ALLOWED_VARS:
            raise _err(f"{rel} 里展开了 {sorted(bad)}（会把 .env 的值展开进提示词）", "只能用 ${STB_OWNER_DISCORD_ID}；要凭证的工具在代码里用到时 credential(\"键名\") 读", "new-task")
        raw = yaml.safe_load(text)                        # 不展开 ${}：和计划原文比（计划里写的也是 ${STB_OWNER_DISCORD_ID}）
        if not isinstance(raw, dict):
            raise _err(f"{rel} 顶层不是映射", "按 builder_guide(\"new-task\") 的例子写成 键: 值", "new-task")
        if raw.get("enabled") is not False:
            raise _err(f"{rel} 的 enabled 是 {raw.get('enabled')!r}（{_line(text, 'enabled')}）", "写 enabled: false（试跑成功后由平台按计划启用）", "new-task")
        if (raw.get("memory") or []) != (k.get("memory") or []):
            raise _err(f"{rel} 的 memory 是 {raw.get('memory')!r}（{_line(text, 'memory')}），计划是 {k.get('memory') or []!r}",
                       f"memory 写成计划里的清单 {k.get('memory') or []}（不读就不写；读记忆要在计划里写明）", "new-task")
        for key, empty in (("listen_events", []), ("listen", None),
                           *((x, None) for x in COMMAND_KEYS)):
            if (raw.get(key) or empty) != (k.get(key) or empty):
                raise _err(f"{rel} 的 {key} 是 {_show(raw.get(key))}（{_line(text, key)}），要和计划一致：计划是 {_show(k.get(key))}",
                           f"写成和计划一样：{key}: {_show(k.get(key))}" + ("（计划没写就删掉这一项）" if k.get(key) in (None, []) else ""), "new-task")
        for key in ("trigger", "schedule", "priority", "runtime", "model"):
            if raw.get(key) != k[key]:
                raise _err(f"{rel} 的 {key} 是 {_show(raw.get(key))}（{_line(text, key)}），计划是 {_show(k[key])}", f"写成 {key}: {_show(k[key])}", "new-task")
        if k.get("timeout") is not None and str(raw.get("timeout")) != str(k["timeout"]):
            raise _err(f"{rel} 的 timeout 是 {_show(raw.get('timeout'))}（{_line(text, 'timeout')}），计划是 {_show(k['timeout'])}", f"写成 timeout: {k['timeout']}", "new-task")
        if raw.get("resumable") != k.get("resumable"):
            raise _err(f"{rel} 的 resumable 是 {_show(raw.get('resumable'))}（{_line(text, 'resumable')}），计划是 {_show(k.get('resumable'))}",
                       f"写成 resumable: {_show(k.get('resumable'))}" if k.get("resumable") is not None else "计划没写就删掉 resumable", "new-task")
        tools = _tools_of(raw, rel)
        if tools != k["tools"]:
            diff = sorted(n for n in set(tools) | set(k["tools"]) if tools.get(n) != k["tools"].get(n))
            raise _err(f"{rel} 的 tools 与计划不一致（{_line(text, 'tools')}）：" + "；".join(f"{n} 实际 {_show(tools.get(n))}，计划 {_show(k['tools'].get(n))}" for n in diff),
                       "tools 照计划逐个写（工具项一模一样，没有工具项的写成 - 名字）", "new-task")
        subs, want_subs = _subagents_of(raw, rel), k.get("subagents") or {}
        if subs != want_subs:
            diff = sorted(n for n in set(subs) | set(want_subs) if subs.get(n) != want_subs.get(n))
            raise _err(f"{rel} 的 subagents 与计划不一致（{_line(text, 'subagents')}）：" + "；".join(f"{n} 实际 {_show(subs.get(n))}，计划 {_show(want_subs.get(n))}" for n in diff),
                       "subagents 照计划逐个写（子代理项一模一样，没有子代理项的写成 - 名字；计划没写就不写 subagents）", "new-task")
        saved = dict(registry.TOOLS)
        registry.TOOLS.update(specs)                     # 新工具还没合入：临时按 check 仓库里的 tool.yaml 校验
        try:
            load_task(f, plan_models(plan), refresh=False)
        except TaskError as e:
            raise _err(f"{rel} 校验失败：{e}", "按报错改那一项（同 stb task check 的规则）", "new-task") from None
        finally:
            registry.TOOLS.clear()
            registry.TOOLS.update(saved)
    _check_changes(root, plan, specs)
    _add_to_check(root, plan, specs)


def _check_subagents(root: Path, plan: dict[str, Any], specs: dict[str, Any]) -> None:
    """计划里新建 / 改的子代理（check 仓库的 subagents/<名>/）：yaml 要和计划一模一样（新建：config；改：origin/main 上的原文 + fields），
    文件夹里正好是 yaml + 新建计划 files 列的文件；改的子代理 files 没列的文件和原来一样（不多不少）；再按子代理规则完整校验（server、hooks 的文件在不在，
    tools 认本计划的新工具）。"""
    from control_center.registry import TOOLS

    known = set(TOOLS) | set(specs)
    for s in plan["plan"].get("subagents") or []:
        name, d = s["name"], root / "subagents" / s["name"]
        rel = f"subagents/{name}/{subagents.YAML_FILE}"
        if not (root / rel).is_file():
            raise _err(f"缺 {rel}", f"把计划里 {name} 的 config 原样写成 {rel}，git commit", "new-subagent")
        if (got := yaml.safe_load((root / rel).read_text())) != s["config"]:
            diff = sorted(k for k in set(got or {}) | set(s["config"]) if (got or {}).get(k) != s["config"].get(k))
            raise _err(f"{rel} 和计划的 config 不一致：{diff} 不对", "照计划的 config 原样写（计划已由 Joy 批准；要改得重新提计划）", "new-subagent")
        have = {f.name for f in d.iterdir() if f.is_file()} - {subagents.YAML_FILE}
        if have != set(s.get("files") or {}):
            raise _err(f"subagents/{name}/ 里的文件和计划的 files 不一致：多了 {sorted(have - set(s.get('files') or {}))}，少了 {sorted(set(s.get('files') or {}) - have)}",
                       "只放计划 files 列的文件（多的 git rm，少的补上），不建子目录", "new-subagent")
        _parse_subagent(root, name, s["config"], known, "new-subagent")
    for c in plan["plan"].get("change_subagents") or []:
        name = c["name"]
        rel = f"subagents/{name}/{subagents.YAML_FILE}"
        want = _subagent_config(plan["plan"], name, root)
        if (got := yaml.safe_load((root / rel).read_text()) or {}) != want:
            diff = sorted(k for k in set(got) | set(want) if got.get(k) != want.get(k))
            raise _err(f"{rel} 和计划不一致：{diff} 不对", "只改计划 fields 里写的字段，值一模一样；别的字段原样不动", "change-subagent")
        base = {Path(x).name for x in git("ls-tree", "--name-only", "origin/main", f"subagents/{name}/", cwd=root).split()} - {subagents.YAML_FILE}
        now = {f.name for f in (root / "subagents" / name).iterdir() if f.is_file()} - {subagents.YAML_FILE}
        for f in sorted((base | now) - set(c.get("files") or {})):
            p = f"subagents/{name}/{f}"
            if f not in base or f not in now or (root / p).read_bytes() != git("show", f"origin/main:{p}", cwd=root).encode():
                raise _err(f"{p} 改了（新建 / 改 / 删），但计划的 files 没列它", f"把 {p} 改回原样；要改得在计划 files 里写明、重新提计划", "change-subagent")
        _parse_subagent(root, name, want, known, "change-subagent")


def _parse_subagent(root: Path, name: str, raw: dict[str, Any], known: set[str], item: str) -> None:
    try:
        d = subagents.parse_def(name, raw, folder=root / "subagents" / name, tools=known)
    except subagents.SubagentError as e:
        raise _err(f"子代理 {name} 不合格：{e}", "按报错改那一项（字段见 stb schema subagents）", item) from None
    if d.isolated or not d.is_editable:
        raise _err(f"子代理 {name} 写了 isolated / is_editable: false", "去掉：隔离子代理只由 Joy 建、改", item)


def task_rel(k: dict[str, Any]) -> str:
    """计划里新任务的定义文件 tasks/<名>/task.yaml（定义监听的 listen.py 放同一文件夹）。"""
    return f"tasks/{k['name']}/task.yaml"


def _listen_same(root: Path, rel: str, want: str | None, item: str, base: bool = False) -> None:
    """listen.py 要和计划里的 listen_py 一模一样（行尾空白不算）；want 为 None 且 base 时要和改动前一样（计划没写改它）。"""
    f = root / rel
    if want is None:
        if not base:
            return
        try:
            old = _base(root, rel)
        except BuilderError:
            old = None
        now = f.read_text() if f.is_file() else None
        if (old or "").rstrip() != (now or "").rstrip():
            raise _err(f"{rel} 改了，但计划没写 listen_py", f"把 {rel} 改回原样；要改得在计划的 listen_py 里写出新内容（审批卡上给 Joy 看）", item)
        return
    if not f.is_file():
        raise _err(f"缺 {rel}", f"照计划的 listen_py 原样写 {rel}，git commit", item)
    if f.read_text().rstrip() != want.rstrip():
        raise _err(f"{rel} 和计划的 listen_py 不一样", "照计划的 listen_py 一字不差地写（Joy 批的是那份）；要改得重新提计划", item)


def _req_same(root: Path, folder: str, deps: list[str] | None, item: str) -> None:
    """folder/requirements.txt 要等于计划的 deps（[] = 没有这个文件）；deps 是 None（改已有的、计划没写 deps）就必须和 main 上一样（依赖环境只按计划重建）。"""
    from control_center.registry import REQUIREMENTS_FILE, _requirements

    rel = f"{folder}/{REQUIREMENTS_FILE}"
    if deps is None:
        try:
            old = _base(root, rel)
        except BuilderError:
            old = None
        if old != ((root / rel).read_text() if (root / rel).is_file() else None):
            raise _err(f"{rel} 改了，但计划没写 deps", f"把 {rel} 改回原样（新建的 git rm）；要改依赖得在计划里写 deps（requirements.txt 的完整新内容），部署时按它重建依赖环境", item)
    elif (got := sorted(_requirements(root / rel))) != sorted(deps):
        raise _err(f"{rel} 是 {_show(got)}，计划的 deps 是 {_show(sorted(deps))}",
                   "requirements.txt 写成和计划 deps 一模一样（一行一个 包==版本；deps 是 [] 或没写就不建这个文件）", item)


def _base(root: Path, rel: str) -> str:
    """check 仓库里 origin/main 上这个文件的原文（改动之前）。"""
    return git("show", f"origin/main:{rel}", cwd=root)


def _declared_diff(rel: str, old: dict[str, Any], new: dict[str, Any], text: str, keys: tuple[str, ...], fields: dict[str, Any]) -> None:
    """计划 fields 里写了的项要等于计划的新值，没写的项不能变（卡上列的就是实际改的）。"""
    for k in keys:
        want = fields[k] if k in fields else _get(old, k)
        if _get(new, k) != want:
            raise _err(f"{rel} 的 {k}（{_line(text, k)}）是 {_show(_get(new, k))}，" + (f"计划 fields 写的是 {_show(want)}" if k in fields else f"计划没写要改它（原来是 {_show(want)}）"),
                       f"改成 {_show(want)}" + ("" if k in fields else "；真要改它得在计划 fields 里写明、重新提计划让 Joy 在卡上看到"),
                       "change-task" if rel.startswith("tasks/") else "change-tool")


def _check_changes(root: Path, plan: dict[str, Any], specs: dict[str, Any]) -> None:
    """change_tools：tool.yaml 照样能解析，TOOL_LOCKED 不变，TOOL_DECLARED 按计划 fields。change_tasks：TASK_DECLARED（含 steps / network / hooks）按计划 fields，
    memory 按计划，tools = 原来的 - tools_remove + tools_add、subagents = 原来的 - subagents_remove + subagents_add（项一模一样），不新引入 ${}（STB_OWNER_DISCORD_ID 除外），
    再用 load_task 完整校验。"""
    from control_center import registry
    from control_center.paths import TASKS, task_file
    from control_center.task import TaskError, load_task

    for c in plan["plan"].get("change_tools") or []:
        rel = tool_files(c["name"])[0]
        text = (root / rel).read_text()
        old, new = yaml.safe_load(_base(root, rel)) or {}, yaml.safe_load(text) or {}
        if bad := [k for k in TOOL_LOCKED if old.get(k) != new.get(k)]:
            raise _err(f"{rel}：改已有工具不能动 {bad}（{_line(text, bad[0])}；usable_by 关系到外部隔离）",
                       f"把 {bad} 改回原样：{json.dumps({k: old.get(k) for k in bad}, ensure_ascii=False)}；真要改请用 ask_user 问 Joy", "change-tool")
        _declared_diff(rel, old, new, text, TOOL_DECLARED, c.get("fields") or {})
        _req_same(root, f"tools/{c['name']}", c.get("deps"), "change-tool")
        try:
            registry.parse_tool_yaml(root / rel, root)
        except ValueError as e:
            raise _err(f"{rel} 解析不过：{e}", "按报错改那一项", "change-tool") from None
    for c in changes_with_retire(plan["plan"]):
        rel = f"tasks/{c['name']}/task.yaml"
        old_text, new_text = _base(root, rel), (root / rel).read_text()
        old, new = yaml.safe_load(old_text) or {}, yaml.safe_load(new_text) or {}
        _declared_diff(rel, old, new, new_text, TASK_DECLARED, c.get("fields") or {})
        _listen_same(root, str(Path(rel).parent / "listen.py"), c.get("listen_py"), "listen", base=True)
        _req_same(root, f"tasks/{c['name']}", c.get("deps"), "change-task")
        want_mem = (old.get("memory") or []) if c.get("memory") is None else c["memory"]
        if (new.get("memory") or []) != want_mem:
            raise _err(f"{rel} 的 memory 是 {new.get('memory')!r}（{_line(new_text, 'memory')}），计划是 {want_mem!r}",
                       f"memory 写成 {want_mem}（[] 就不写）；要改读记忆得在计划的 memory 里写明", "change-task")
        if bad := set(re.findall(r"\$\{([^}]*)\}", new_text)) - set(re.findall(r"\$\{([^}]*)\}", old_text)) - ALLOWED_VARS:
            raise _err(f"{rel} 新引入了 ${{}} {sorted(bad)}（会把 .env 的值展开进任务）", "只能用 ${STB_OWNER_DISCORD_ID}；去掉这些 ${}", "change-task")
        want = {**{k: v for k, v in _tools_of(old, rel).items() if k not in c.get("tools_remove", [])}, **(c.get("tools_add") or {})}
        if (got := _tools_of(new, rel)) != want:
            diff = sorted(n for n in set(got) | set(want) if got.get(n) != want.get(n))
            raise _err(f"{rel} 的 tools 与计划不一致（{_line(new_text, 'tools')}）：" + "；".join(f"{n} 实际 {_show(got.get(n))}，按计划应为 {_show(want.get(n))}" for n in diff),
                       "tools = 原来的 - tools_remove + tools_add（工具项一模一样），照这个改", "change-task")
        want = {**{k: v for k, v in _subagents_of(old, rel).items() if k not in c.get("subagents_remove", [])}, **(c.get("subagents_add") or {})}
        if (got := _subagents_of(new, rel)) != want:
            diff = sorted(n for n in set(got) | set(want) if got.get(n) != want.get(n))
            raise _err(f"{rel} 的 subagents 与计划不一致（{_line(new_text, 'subagents')}）：" + "；".join(f"{n} 实际 {_show(got.get(n))}，按计划应为 {_show(want.get(n))}" for n in diff),
                       "subagents = 原来的 - subagents_remove + subagents_add（子代理项一模一样），照这个改", "change-task")
        saved = dict(registry.TOOLS)
        registry.TOOLS.update(specs)
        try:
            load_task(root / rel, plan_models(plan), refresh=False)
        except TaskError as e:
            raise _err(f"{rel} 校验失败：{e}", "按报错改那一项（同 stb task check 的规则）", "change-task") from None
        finally:
            registry.TOOLS.clear()
            registry.TOOLS.update(saved)


def prepare_check(pid: str) -> dict[str, Any]:
    """取计划分支（容器里 git bundle，宿主不在开发区里跑 git）→ 新克隆 main 成 check 仓库 → 范围检查 → 在当前 main 上做成一个提交。"""
    p = load_plan(pid)
    work = _dir() / pid
    bundle = work / "branch.bundle"
    work.mkdir(parents=True, exist_ok=True, mode=0o750)
    bundle.write_bytes(_ok(_drun(container(pid, p, ["git", "-C", "/repo", "bundle", "create", "-", f"builder/{pid}"]), timeout=300), "取计划分支"))
    check = work / "check"
    shutil.rmtree(check, ignore_errors=True)
    git("clone", "-q", "--branch", "main", str(REPO), str(check), cwd=work)
    git("fetch", "-q", str(bundle), f"builder/{pid}:refs/heads/plan", cwd=check)
    r = _run(["git", "merge-base", "origin/main", "plan"], cwd=check)
    base = r.stdout.decode().strip()
    if r.returncode != 0 or not base:
        raise _err("计划分支和 main 没有共同祖先", f"在开发区克隆的分支 builder/{pid} 上提交（git checkout builder/{pid}），不能新建根提交 / 换分支")
    changes = []
    toks = git("diff", "--raw", "-z", "--no-renames", "--no-abbrev", base, "plan", cwd=check).split("\0")
    for meta, path in zip(toks[0::2], toks[1::2]):     # -z：":旧模式 新模式 旧sha 新sha 状态" 与路径交替
        _old_mode, new_mode, _old, new_sha, st = meta.lstrip(":").split()
        changes.append((st, path, new_mode, new_sha))
    if not changes:
        raise _err(f"计划分支 builder/{pid} 相对 main 没有任何改动", "builder_dev_run([\"git\", \"add\", \"-A\"]) 再 builder_dev_run([\"git\", \"commit\", \"-m\", \"…\"])")
    out = [c[1] for c in changes if not _allowed(c[1], p)]
    if locked := sorted({w for x in out if (w := _locked_owner(x))}):
        note = "；".join(LOCKED_NOTE.format(what=w) for w in locked)
        raise _err(f"{note}（改到了：{'、'.join(x for x in out if _locked_owner(x))[:500]}）",
                   f"把这些文件改回 main 的原样（git checkout main -- <文件>，新建的 git rm）再 commit；把这句原话告诉 Joy：「{note}」", "change-tool")
    if sub := [x for x in out if Path(x).parts[:1] == ("subagents",)]:
        raise _err("改了计划没列的子代理（只能动计划 subagents / change_subagents 列的 subagents/<名>/ 里的文件，不进子目录）：" + "、".join(sub[:10]),
                   "git rm（新建的）/ git checkout main -- <文件>（改过的），改回原样再 commit", "change-subagent")
    if out:
        raise _err("改动超出计划范围：" + "、".join(out[:10]),
                   "只能改 tools/<计划工具>/、tasks/<计划任务>/、change_tools 列的工具的文件夹、change_tasks 列的任务的文件夹 tasks/<名>/、"
                   "subagents/<计划子代理>/、tests/stb_platform/test_<计划里的名字>.py；别的文件用 git checkout main -- <文件> 改回（新建的 git rm），再 commit", "new-tool")
    for st, path, mode, sha in changes:
        if st != "D" and mode not in ("100644", "100755"):
            raise _err(f"{path} 是符号链接或子模块", "换成普通文件（git rm 后重新写）")
        if st != "D" and (size := int(git("cat-file", "-s", sha, cwd=check).strip())) > MAX_FILE_BYTES:
            raise _err(f"{path} 有 {size} 字节，超过 1 MB", "拆小，或大文件（模型、数据）别放仓库：运行时下载到 STB_STATE_DIR")
    deleted = {c[1] for c in changes if c[0] == "D"}
    for what, files in retire_files(p["plan"]).items():
        if left := [x for x in files if x not in deleted]:
            raise _err(f"下线{what}：这些文件还没删：{'、'.join(left[:10])}",
                       f"builder_dev_run([\"git\", \"rm\", \"-r\", \"-q\", \"--\", …]) 删掉它们再 commit（卡上列的文件都要删）", "retire")
    paths = [c[1] for c in changes]
    if clash := git("diff", "--name-only", base, "origin/main", "--", *paths, cwd=check).split():
        raise _err(f"main 在分叉之后也改了这些文件：{clash[:5]}", "builder_dev_run([\"git\", \"diff\", …]) 看不到新 main；这是平台那边的冲突，用 ask_user 问 Joy 怎么合")
    git("checkout", "-q", "-B", "deploy", "origin/main", cwd=check)
    if keep := [c[1] for c in changes if c[0] != "D"]:
        git("checkout", "plan", "--", *keep, cwd=check)
    if gone := [c[1] for c in changes if c[0] == "D"]:
        git("rm", "-q", "--", *gone, cwd=check)
    scan_values(check, keep, p["plan"])
    check_isolation(check, p["plan"])
    check_tree(check, p)
    purpose = re.sub(r"\s+", " ", p["plan"]["purpose"])[:30]
    git("-c", "user.name=stb-builder", "-c", "user.email=builder@sparky", "commit", "-q", "-m", f"feat(builder): 部署计划 {pid} {purpose}", cwd=check)
    ahead = git("log", "--format=%s", f"{base}..origin/main", cwd=check).splitlines()   # 开发区建好（分叉）之后 main 新增的提交
    return {"check": str(check), "commit": git("rev-parse", "HEAD", cwd=check).strip(), "files": paths, "main_ahead": ahead}


def check_isolation(root: Path, plan: dict[str, Any]) -> None:
    """计划动到的工具 / 子代理 / 任务文件夹（check 仓库里合入后的样子）自给自足：代码只 import 平台（control_center）、标准库与依赖、自己文件夹里的模块（registry.folder_imports）。"""
    from control_center.registry import folder_imports

    tasks = [("tasks", t["name"]) for t in plan.get("tasks") or []] + [("tasks", c["name"]) for c in plan.get("change_tasks") or []]
    for kind, name in [*plan_folders(plan), *tasks]:
        if (root / kind / name).is_dir() and (bad := folder_imports(root / kind / name, root)):
            raise _err(f"{kind}/{name}/ 用了别的文件夹的代码：" + "；".join(bad[:5]),
                       "要用的函数复制进自己文件夹（只留用到的），import 改成自己文件夹里的；平台的接口从 control_center import（tool_kit、credential、media 等）", "new-tool")


def scan_values(root: Path, paths: list[str], plan: dict[str, Any]) -> None:
    """计划改动的文件（新工具、新任务、测试、change_tools / change_tasks 改到的）和计划里的 models 档位：不能原样写着 .env 的值（env.check_no_values）。"""
    try:
        for rel in paths:
            env.check_no_values((root / rel).read_bytes().decode(errors="replace"), rel)
        for m in plan.get("models") or []:
            env.check_no_values(yaml.safe_dump(m["profile"], allow_unicode=True, sort_keys=False), f"计划 models 的档位 {m['name']}")
    except env.EnvError as e:
        raise _err(str(e), "把写死的值删掉：工具用到时 credential(\"键名\") 读 .env（from control_center.credential import credential）；任务里只能用 ${STB_OWNER_DISCORD_ID}",
                   "credential-tool") from None


def run_tests(pid: str, check: Path) -> str:
    """合入后的代码在断网容器里跑全部 tests/stb_platform（agent 写的测试以 uid 1001 跑，碰不到 joyzhang14 的东西）。
    没过就把 pytest 的原始输出（失败用例的断言与 traceback）交回。"""
    p = load_plan(pid)
    cmd = container(pid, p, ["sh", "-c", "cp -r /src/. /repo/ && rm -rf /repo/.git && python -m pytest tests/stb_platform -q -p no:cacheprovider"],
                    src=check, memory="8g")
    t0 = time.time()
    try:
        r = _drun(cmd, timeout=TEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise _err(f"全部测试跑了 {time.time() - t0:.0f} 秒，到了上限 {TEST_TIMEOUT_S} 秒还没跑完，容器已停",
                   "找出慢的测试（builder_dev_run([\"python\", \"-m\", \"pytest\", \"tests/stb_platform/test_<名>.py\", \"--durations=10\"])），"
                   "别在测试里联网、sleep 或真跑模型") from None
    out = r.stdout.decode(errors="replace")
    summary = (out.strip().splitlines() or ["（没有输出）"])[-1]
    if r.returncode != 0:
        err = r.stderr.decode(errors="replace").strip()
        raise _err(f"测试没过：{summary}\npytest 的原始输出（尾部）：\n{out[-8000:]}" + (f"\nstderr：\n{err[-2000:]}" if err else ""),
                   "照上面失败用例的断言 / traceback 改代码或测试；用 builder_dev_run([\"python\", \"-m\", \"pytest\", \"<失败的测试>\", \"-q\"]) 单独复现，"
                   "改完 git commit 再 builder_verify；失败的是平台原有的测试、和你的改动无关时用 ask_user 问 Joy")
    return summary


def merge(check: Path, commit: str, paths: list[str]) -> None:
    if git("rev-parse", "--abbrev-ref", "HEAD").strip() != "main":
        raise _err("~/sparky-toolbox 当前不在 main 分支（平台那边的状态，不是你的代码）", "不用改代码：用 ask_user 告诉 Joy，等他处理后再 builder_submit")
    if git("status", "--porcelain", "--untracked-files=no").strip():
        raise _err("~/sparky-toolbox 的 main 有未提交的改动，不合入（平台那边的状态，不是你的代码）", "不用改代码：用 ask_user 告诉 Joy，等他处理后再 builder_submit")
    if git("status", "--porcelain", "--", *paths).strip():
        raise _err("~/sparky-toolbox 里要写的路径已有未跟踪的文件，不合入", "换个文件名，或用 ask_user 告诉 Joy（列出这些路径）")
    git("fetch", "-q", str(check), commit)
    git("merge", "--ff-only", "-q", commit)


def install_runtime(pid: str, plan: dict[str, Any]) -> None:
    """合入后：计划里有依赖的工具 / 任务（新的，和改了依赖的已有的，dep_owners）在断网容器里用 wheelhouse 离线重建依赖环境（_install_venv：
    data/runtime/venvs/tools|tasks/<名>），改成没有依赖（deps: []）的删掉旧环境；新工具建状态目录 data/tool_state/<名>（代码就用仓库里的）；
    工具目录里有 Dockerfile 的，请 stbcc-docker 建镜像 stb-<工具名>（要写在它 tool.yaml 的 docker.images 里）。"""
    for kind, name, deps in dep_owners(plan["plan"]):
        _install_venv(pid, name, deps, kind)
    for kind, key in (("tools", "change_tools"), ("tasks", "change_tasks")):
        for c in plan["plan"].get(key) or []:
            if c.get("deps") == []:
                shutil.rmtree(venv_dir(c["name"], kind), ignore_errors=True)
    for t in plan["plan"]["tools"]:
        agent_state(t["name"])
        if (REPO / "tools" / t["name"] / "Dockerfile").is_file():
            _ok(_dbuild(t["name"]), f"建 {t['name']} 的镜像")
    sudo("rm", "-rf", str(_stage_home(pid)))


def _stb(*argv: str, timeout: float = 600) -> str:
    return _ok(_run([str(Path(sys.executable).parent / "stb"), *argv], timeout=timeout, cwd=REPO), f"stb {' '.join(argv[:2])}").decode(errors="replace")


def _notify(text: str) -> None:
    from control_center import hub_client

    for _ in range(12):                                  # stbcc-hub 刚重启时要等它起来
        r = asyncio.run(hub_client.call("send", text=text))
        if r and r.get("ok"):
            return
        time.sleep(10)


def verify() -> dict[str, Any]:
    """builder_verify：按部署检查的全部规则检查当前开发区（不合入、不跑 install.py、不发卡），任何时候可调用、次数不限。
    依次：有没有没提交的改动 → 范围、字段、load_task / registry 解析、凭证值扫描（prepare_check）→ 全部测试 → 真实运行环境试跑。
    范围检查不过就停在那（后面的检查要它的结果）；测试和试跑都跑、错误一起返回。每个错误是原始报错或带改法的整理报错。"""
    p = _ready()
    pid = p["id"]
    errors, passed = [], []
    r = _drun(container(pid, p, ["git", "status", "--porcelain"]), timeout=120)
    if r.returncode == 0 and (dirty := r.stdout.decode(errors="replace").strip()):
        errors.append(f"开发区有没提交的改动（部署只看分支 builder/{pid} 上已提交的内容）：\n{dirty[:2000]}\n"
                      "改法：builder_dev_run([\"git\", \"add\", \"-A\"]) 再 builder_dev_run([\"git\", \"commit\", \"-m\", \"…\"])，然后重新 builder_verify")
    try:
        info = prepare_check(pid)
    except (BuilderError, OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "errors": [*errors, str(e)], "passed": passed}
    passed.append("范围、字段、任务加载、凭证值扫描：通过")
    for name, fn in (("测试", lambda: run_tests(pid, Path(info["check"]))), ("真实运行环境试跑", lambda: pre_merge_trial(pid, p, Path(info["check"])))):
        try:
            passed.append(f"{name}：{fn()}")
        except (BuilderError, OSError, subprocess.TimeoutExpired) as e:
            errors.append(str(e))
    out: dict[str, Any] = {"ok": not errors, "errors": errors, "passed": passed}
    if ahead := info.get("main_ahead"):
        out["note"] = (f"main 已更新（开发区建好后 main 新增 {len(ahead)} 个提交：{'；'.join(ahead[:10])}{'…' if len(ahead) > 10 else ''}）。"
                       "开发区是旧 main 的副本，在开发区里（builder_dev_run）不过的测试如果来自你改动之外的代码，以 builder_verify / builder_submit 的测试为准："
                       "它们都把你的改动接到当前 main 上重跑")
    if not errors:
        out["next"] = "全部通过：builder_submit 交给平台部署" + ("（部署时合入之前平台还会跑文件夹里的 install.py，这一步只在部署时做）"
                                                        if any((Path(info["check"]) / k / n / INSTALL_FILE).is_file() for k, n in plan_folders(p["plan"])) else "")
    return out


def submit() -> dict[str, Any]:
    """builder_submit：同步做到合入——范围检查 → 测试 → 真实运行环境试跑 → 文件夹里的 install.py → ff 合入 main（main 在这期间被别人动了就按新 main 重做，最多 3 次）。
    合入之前任何失败都不合入、计划回到 ready，报错直接交回子代理（抛 BuilderError）。合入之后的事交给 worker（post_merge），这里立即返回。"""
    p = _ready()
    pid = p["id"]
    save_plan(p, "deploying")
    p["cleanup"] = retire_cleanup(p["plan"])              # 下线工具的运行环境：合入前按现在的工具表算（合入后工具就不在表里了）
    installed: list[str] | None = None
    try:
        for attempt in range(3):
            info = prepare_check(pid)
            _log(pid, f"范围检查通过（第 {attempt + 1} 次），提交 {info['commit'][:10]}，文件 {info['files']}")
            tests = run_tests(pid, Path(info["check"]))
            _log(pid, f"测试：{tests}")
            tried = pre_merge_trial(pid, p, Path(info["check"]))
            _log(pid, f"真实运行环境试跑通过：{tried}")
            if installed is None:
                installed = run_installs(pid, p, Path(info["check"]))   # 合入前：文件夹里的 install.py；不过已自己撤回
            retire_busy(p["plan"])                         # 要注销的任务在跑 / 在队列里：不合入
            try:
                merge(Path(info["check"]), info["commit"], info["files"])
                break
            except BuilderError as e:
                if "ff" not in str(e).lower() and "fast-forward" not in str(e).lower():
                    raise
                _log(pid, f"main 动了，重做：{e}")
        else:
            raise _err("main 一直在变，3 次都没能快进合入", "不用改代码：过一会儿再 builder_submit")
    except (BuilderError, subprocess.TimeoutExpired, OSError) as e:
        p["deploy_failures"] = int(p.get("deploy_failures", 0)) + 1
        p.pop("cleanup", None)
        save_plan(p, "ready", f"部署失败：{str(e)[:2000]}")
        _log(pid, f"失败：{e}")
        err = str(e) if len(str(e)) <= 12000 else str(e)[:12000] + f"\n…（后面省略 {len(str(e)) - 12000} 字；完整报错在 data/hub/builder/{pid}/deploy.log，Joy 能看）"
        raise BuilderError(f"部署失败，什么都没合入（第 {p['deploy_failures']} 次）：\n{err}\n"
                           "按报错里的改法改（builder_try_tool 能在真实运行环境里先试），builder_verify 全过后再 builder_submit；"
                           "尽力试过都走不通，就用 ask_user 问 Joy，附上报错和你试过的改法") from None
    _log(pid, f"已合入 main {info['commit'][:10]}")
    p["merged"] = {"commit": info["commit"], "files": info["files"], "tests": tests, "installed": installed or []}
    save_plan(p, "merged")
    try:
        spawn("post", pid)
    except (BuilderError, OSError, subprocess.TimeoutExpired) as e:
        _notify(f"⚠ builder 计划 {pid} 已合入 main（{info['commit'][:10]}），但起不了合入后的 worker：{e}（要 Joy 手动 stb builder worker post {pid}）")
    return {"plan": pid, "status": "merged", "commit": info["commit"][:10], "files": info["files"], "tests": tests, "installed": installed or [],
            "note": "已合入 main。平台接着在后台装运行环境、加进任务、试跑新任务、按计划上线、重启服务，结果私信 Joy；你的开发到这里结束，把结果告诉调用你的主模型"}


def post_merge(pid: str) -> str:
    """worker（合入之后，独立的 systemd 单元）：写新档位 → 装运行环境 → 凭证检查 → 加进任务 → 新任务登记、试跑、按计划启用 → 注销下线的任务、清运行环境
    → 改了的任务重新登记 → 重启服务。出错的写进给 Joy 的总结，任务保持停用。返回给 Joy 的总结。"""
    p = load_plan(pid)
    if p["status"] != "merged":
        raise BuilderError(f"计划 {pid} 是 {p['status']}，不是 merged")
    m = p.get("merged") or {}
    lines = [f"🧱 builder 计划 {pid} 已合入 main（{str(m.get('commit', ''))[:10]}）", "改动：" + "、".join(m.get("files") or []), f"测试：{m.get('tests', '')}",
             *(m.get("installed") or [])]
    try:
        if tiers := write_tiers(pid, p["plan"]):
            _log(pid, f"models.yaml 加了档位 {tiers}")
            lines.append(f"models.yaml 加了档位：{'、'.join(tiers)}（原文备份在 data/hub/builder/{pid}/models.yaml.before）")
        install_runtime(pid, p)
        p["unchecked"] = []
        for t in p["plan"]["tools"]:
            if tool_credentials(REPO / "tools" / t["name"]):
                ok, out = asyncio.run(check_tool(p, t["name"]))
                _log(pid, f"凭证检查 {t['name']}：{'通过' if ok else '没过'}")
                lines.append(f"凭证检查 {t['name']}：{'通过' if ok else '没过'}：{out[:300]}")
                if not ok:
                    p["unchecked"].append(t["name"])
        save_plan(p)
        lines += add_to_tasks(p, {t["name"] for t in p["plan"]["tools"]} - set(p["unchecked"]))
        if p["unchecked"]:
            lines.append(f"没过检查的 {p['unchecked']} 先不加进任务：Joy 重填凭证后由 builder 用 builder_check 验证、再加进任务")
        for k in p["plan"]["tasks"]:
            _stb("task", "add", task_rel(k))
            out = _stb("run", k["name"], "--now", "--wait", timeout=6 * 3600)
            st = json.loads(out[out.index("{"):]).get("status")
            _log(pid, f"试跑 {k['name']}：{st}")
            if st != "complete":
                lines.append(f"试跑 {k['name']}：{st}，保持停用（修要新计划）")
                continue
            if k["enabled_after_deploy"]:
                _stb("task", "enable", k["name"])
            lines.append(f"试跑 {k['name']}：complete，{'已上线' if k['enabled_after_deploy'] else '按计划保持停用'}")
        for r in p["plan"].get("retire_tasks") or []:     # 下线的任务：定义已随合入删掉，注销登记；运行记录、工作目录保留
            _stb("task", "rm", r["name"], "--yes", "--keep-data")
            lines.append(f"已注销任务 {r['name']}（stb task rm --keep-data：运行记录与工作目录保留）")
            if (v := venv_dir(r["name"], "tasks")).is_dir() and re.fullmatch(NAME_RE, r["name"]):
                shutil.rmtree(v)
                lines.append(f"已清掉任务 {r['name']} 的依赖环境 {v}")
        if p["plan"].get("retire_tools"):
            lines.append(f"已下线工具：{'、'.join(r['name'] for r in p['plan']['retire_tools'])}")
            lines += clean_retired(p.get("cleanup") or {})
        for c in p["plan"].get("change_tasks") or []:     # 改了触发方式 / 启停的已有任务：按新的 yaml 重新登记
            if c.get("fields"):
                _stb("task", "add", f"tasks/{c['name']}/task.yaml")
                lines.append(f"已按新定义重新登记 {c['name']}（{'、'.join(c.get('fields', {}))}）")
        if bad := end_plan(p, "deployed"):
            lines.append(f"⚠ 中间产物有没删掉的：{'；'.join(bad)}")
    except (BuilderError, subprocess.TimeoutExpired, OSError, ValueError) as e:
        _log(pid, f"合入后出错：{e}")
        lines.append(f"⚠ 合入后出错：{e}（任务保持停用）")
        if bad := end_plan(p, "failed", f"合入后出错：{e}"):
            lines.append(f"⚠ 中间产物有没删掉的：{'；'.join(bad)}")
    from control_center import systemd

    # 不等空闲：每次运行在自己的运行单元里，runner 重启不打断它们（新 runner 按 data/runtime/runner.json 接管）；
    # 等卡的运行在网关里按 data/hub/approvals 的文件等，stbcc-hub / stbcc-discord 重启后卡照样能点
    systemd.systemctl("restart", "stbcc-runner", "stbcc-hub", "stbcc-web", "stbcc-discord", "stbcc-builder", check=False)
    lines.append("已重启 stbcc-runner / stbcc-hub / stbcc-web / stbcc-discord / stbcc-builder（在跑的运行不受影响）")
    return "\n".join(lines)


def prepare(pid: str) -> tuple[bool, str]:
    """建开发区（克隆、wheelhouse、deps）：好了计划转 ready、返回 (True, "")；失败计划转 failed、返回 (False, 原因)。"""
    p = load_plan(pid)
    save_plan(p, "preparing")
    d, work = dev_dir(pid), _dir() / pid
    work.mkdir(parents=True, exist_ok=True, mode=0o750)
    try:
        sudo("mkdir", "-p", str(d / "wheelhouse"), str(d / "deps"))
        bundle = work / "main.bundle"
        git("bundle", "create", str(bundle), "main")
        sudo("tee", str(d / "main.bundle"), input=bundle.read_bytes())
        sudo("git", "clone", "-q", "-b", "main", str(d / "main.bundle"), str(d / "repo"))
        if SHALLOW_DEV:                              # 截成浅克隆：main 最新提交作边界，之前的历史清掉；提交号不变，分支照样能快进合入
            head = sudo("git", "-C", str(d / "repo"), "rev-parse", "HEAD").decode().strip()
            sudo("tee", str(d / "repo" / ".git" / "shallow"), input=(head + "\n").encode())
            sudo("git", "-C", str(d / "repo"), "reflog", "expire", "--expire=now", "--all")
            sudo("git", "-C", str(d / "repo"), "gc", "-q", "--prune=now")
        sudo("git", "-C", str(d / "repo"), "checkout", "-q", "-b", f"builder/{pid}")
        sudo("rm", "-f", str(d / "main.bundle"))
        wh = work / "wheelhouse"
        wh.mkdir(parents=True, exist_ok=True)
        owners = [(_deps_target(kind, name), deps) for kind, name, deps in dep_owners(p["plan"])]
        if owners:                                       # 有依赖：平台依赖的锁定版本和计划依赖一起下（pip 一起解析，和平台冲突就在这里报错）
            (wh / PLATFORM_REQS).write_text(platform_requirements())
            _ok(_run(["/usr/bin/python3", "-m", "pip", "download", "-q", "--only-binary=:all:", "--no-cache-dir", "-d", str(wh),
                      "-r", str(wh / PLATFORM_REQS), *(x for _, deps in owners for x in deps)], timeout=900), "下载依赖（只收 wheel，平台的依赖一起下）")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            tar.add(wh, arcname=".")
        sudo("tar", "-x", "-C", str(d / "wheelhouse"), "-f", "-", input=buf.getvalue())
        for target, deps in owners:                      # 开发区：平台的依赖在仓库 venv 里，这里只装计划依赖（版本受平台锁定版本约束）
            _ok(_drun(container(pid, p, ["/usr/bin/python3", "-m", "pip", "install", "-q", "--no-index", "--break-system-packages", "--find-links", "/wheelhouse",
                                        "-c", f"/wheelhouse/{PLATFORM_REQS}", "--target", target, *deps], deps_rw=True), timeout=900), f"装 {target} 的依赖")
    except (BuilderError, subprocess.TimeoutExpired, OSError) as e:
        end_plan(p, "failed", f"准备开发区失败：{e}")
        return False, f"Joy 已批准计划 {pid}，但准备开发区失败：{e}（平台的问题：告诉 Joy）"
    save_plan(p, "ready")
    return True, ""


def worker(op: str, pid: str) -> None:
    """`stb builder worker post <id>`（submit 用 systemd-run 起）：合入之后的事做完私信 Joy。"""
    try:
        if op != "post":
            raise BuilderError(f"不认识的 worker {op!r}（只有 post）")
        text = post_merge(pid)
    except Exception as e:  # noqa: BLE001 — 任何意外都要让 Joy 知道
        text = f"❌ builder {op} {pid} 出错：{type(e).__name__}: {str(e)[:500]}"
    print(text, flush=True)
    _notify(text)
