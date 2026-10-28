"""记忆写入与加载时的威胁模式扫描（规格 11.8「污染与安全」、D16）。

移植自 Hermes Agent `tools/threat_patterns.py`（https://github.com/NousResearch/hermes-agent ，提交 8ba5fe9c），
记忆写入用它的 strict 扫描，即全部模式。本平台的改动：
- `~/.hermes/.env` → `~/.stb/secrets` 与仓库根目录 `.env`（hermes_env → stb_secrets）；`.hermes/config.yaml|SOUL.md` → 任务 YAML 与 models.yaml（stb_config_mod）；
  env_var_unset_agent 加 STB；send_to_url 的英文动词加 forward。
- 中文触发词：send_to_url_zh（发送 / 发到 / 发给 / 上传 / 转发 … 网址）、prompt_injection_zh（忽略 … 之前的指令）。
  汉字之间没有 `\\b` 词边界，所以中文模式不加 `\\b`。
- 本平台补充（原文件没有，D15「密钥扫描保留」）：已知密钥前缀、私钥头、Discord token、不带引号的 `token=…` 赋值。

---- 原文件许可 ----
MIT License

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

import re
import unicodedata

# 扫描文本上限：扫描是防御性的，限住最坏耗时。
MAX_SCAN_CHARS = 65_536
# 关键词之间的有界填充（无界的 (?:\w+\s+)* 回溯很慢）。
_FILLER = r"(?:\w+\s+){0,8}"
# 以密钥类后缀结尾的环境变量引用。
_SECRET_VAR = r"\$\{?\w*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)S?\b"
# "改 agent 配置"类模式的动词前缀。
_MODIFY = r"(update|modify|edit|write|change|append|add\s+to)\s+[^\n]{0,2048}"

# (正则, 模式名)。原文件的 scope 字段在这里不需要：记忆写入与加载都按 strict，全部模式参与。
_PATTERNS: list[tuple[str, str]] = [
    # ── 经典提示注入 ──
    (rf'ignore\s+{_FILLER}(previous|all|above|prior)\s+{_FILLER}instructions', "prompt_injection"),
    (r'system\s+prompt\s+override', "sys_prompt_override"),
    (rf'disregard\s+{_FILLER}(your|all|any)\s+{_FILLER}(instructions|rules|guidelines)', "disregard_rules"),
    (rf'act\s+as\s+(if|though)\s+{_FILLER}you\s+{_FILLER}(have\s+no|don\'t\s+have)\s+{_FILLER}(restrictions|limits|rules)', "bypass_restrictions"),
    (r'<!--[^>]{0,512}(?:ignore|override|system|secret|hidden)[^>]{0,512}-->', "html_comment_injection"),
    (r'<\s*div\s+style\s*=\s*["\'][^>]{0,2048}display\s*:\s*none', "hidden_div"),
    (r"translate\s+[^\n]{0,512}\s+into\s+\w+(?:[\s-]+\w+){0,2}\s+and\s+(execute|run|eval)\b", "translate_execute"),
    (rf'do\s+not\s+{_FILLER}tell\s+{_FILLER}the\s+user', "deception_hide"),

    # ── 角色劫持 ──
    (rf'you\s+are\s+{_FILLER}now\s+(?:a|an|the)\s+', "role_hijack"),
    (rf'pretend\s+{_FILLER}(you\s+are|to\s+be)\s+', "role_pretend"),
    (rf'output\s+{_FILLER}(system|initial)\s+prompt', "leak_system_prompt"),
    (rf'(respond|answer|reply)\s+without\s+{_FILLER}(restrictions|limitations|filters|safety)', "remove_filters"),
    (rf'you\s+have\s+been\s+{_FILLER}(updated|upgraded|patched)\s+to', "fake_update"),
    (r'\bname\s+yourself\s+\w+', "identity_override"),

    # ── C2 / promptware ──
    (r'register\s+(as\s+)?a?\s*node', "c2_node_registration"),
    (r'(heartbeat|beacon|check[\s\-]?in)\s+(to|with)\s+', "c2_heartbeat"),
    (r'pull\s+(down\s+)?(?:new\s+)?task(?:ing|s)?\b', "c2_task_pull"),
    (r'connect\s+to\s+the\s+network\b', "c2_network_connect"),
    (r'you\s+must\s+(?:\w+\s+){0,3}(register|connect|report|beacon)\b', "forced_action"),
    (r'only\s+use\s+one[\s\-]?liners?\b', "anti_forensic_oneliner"),
    (rf'never\s+{_FILLER}(?:create|write)\s+{_FILLER}(?:script|file)\s+{_FILLER}disk', "anti_forensic_disk"),
    (r'unset\s+\w*(?:CLAUDE|CODEX|HERMES|AGENT|OPENAI|ANTHROPIC|STB)\w*', "env_var_unset_agent"),
    (r'\b(?:cobalt\s*strike|sliver|havoc|mythic|metasploit|brainworm)\b', "known_c2_framework"),
    (r'\bc2\s+(?:server|channel|infrastructure|beacon)\b', "c2_explicit"),
    (r'\bcommand\s+and\s+control\b', "c2_explicit_long"),

    # ── 外传 ──
    (rf'curl\s+[^\n]{{0,2048}}{_SECRET_VAR}', "exfil_curl"),
    (rf'wget\s+[^\n]{{0,2048}}{_SECRET_VAR}', "exfil_wget"),
    (r'cat\s+[^\n]{0,2048}(\.env|credentials|\.netrc|\.pgpass|\.npmrc|\.pypirc)', "read_secrets"),
    (r'(send|post|upload|transmit|forward)\s+[^\n]{0,2048}\s+(to|at)\s+https?://', "send_to_url"),
    (r'(发送|发到|发给|发往|上传|转发|推送)[^\n]{0,2048}https?://', "send_to_url_zh"),
    (rf'(include|output|print|share)\s+{_FILLER}(conversation|chat\s+history|previous\s+messages|full\s+context|entire\s+context)', "context_exfil"),
    (r'(忽略|无视|不要理会)[^\n]{0,40}(之前|以上|上面|前面|所有|全部)[^\n]{0,40}(指令|指示|规则|要求)', "prompt_injection_zh"),

    # ── 持久化 / SSH 后门 ──
    (r'authorized_keys', "ssh_backdoor"),
    (r'(?:\b(?:echo|cat|cp|mv|dd|tee|install|printf|rsync|scp|ln|append|add|write'
     r'|sed|chmod|chown|truncate|rm|touch|curl|wget|git)\b|\bopen\s*\(|>>?)'
     r'[^\n]{0,512}(?:\$HOME/\.ssh|~/\.ssh)', "ssh_access"),
    (r'(?:\$HOME|~)/\.stb/secrets|\.stb/secrets/|sparky-toolbox/\.env\b|sparky-toolbox/data/(?:tool_state/)?owa/launcher\.token', "stb_secrets"),   # 凭证：仓库根目录 .env（旧位置 ~/.stb/secrets）
    (rf'{_MODIFY}(?:AGENTS\.md|CLAUDE\.md|\.cursorrules|\.clinerules)', "agent_config_mod"),
    (rf'{_MODIFY}(?:\.stb/(?:tasks/|models\.yaml|registered\.json)|data/(?:models\.yaml|registered\.json|runtime/registered\.json)|config/(?:models|stbcc)\.yaml|tasks/[\w-]+(?:/task)?\.ya?ml)', "stb_config_mod"),

    # ── 硬编码密钥 ──
    (r'(?:api[_-]?key|token|secret|password)\s*[=:]\s*["\']'
     r'(?!(?-i:[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+)["\'])'
     r'[A-Za-z0-9+/=_-]{20,}', "hardcoded_secret"),

    # ── 本平台补充（原文件没有）──
    (r'(?:api[_-]?key|token|secret|password|passwd)\s*[=:]\s*[A-Za-z0-9+/=_\-]{16,}', "secret_assignment"),
    (r'-----BEGIN [A-Z ]*PRIVATE KEY-----', "private_key"),
    (r'\b(?:sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{30,})', "known_key_prefix"),
    (r'\b[MN][A-Za-z\d]{23,}\.[\w-]{6}\.[\w-]{27,}', "discord_token"),
]

# 注入常用的不可见 / 双向 Unicode。
INVISIBLE_CHARS = frozenset(
    "\u200b\u200c\u200d\u2060\u2062\u2063\u2064\ufeff"
    "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")

_COMPILED = [(re.compile(p, re.IGNORECASE), pid) for p, pid in _PATTERNS]


def scan_for_threats(content: str) -> list[str]:
    """命中的模式名；不可见字符报成 invisible_unicode_U+XXXX。"""
    if not content:
        return []
    content = content[:MAX_SCAN_CHARS]
    # 不可见字符在原文上查：NFKC 会把它们去掉。
    findings = [f"invisible_unicode_U+{ord(ch):04X}" for ch in sorted(set(content) & INVISIBLE_CHARS)]
    # NFKC 把全角等兼容字符折回（ｃａｔ → cat），防同形绕过；不处理跨文字的混淆字符。
    normalised = unicodedata.normalize("NFKC", content)
    findings.extend(pid for pat, pid in _COMPILED if pat.search(normalised))
    return findings


def first_threat(content: str) -> str | None:
    found = scan_for_threats(content)
    return found[0] if found else None
