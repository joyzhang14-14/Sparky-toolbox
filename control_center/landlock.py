"""Landlock（Linux 的无特权进程级访问限制）：restrict 只限制联网（禁 TCP，可放行本机几个端口），不管文件——
文件读写看运行身份（sparky 用户）本身的文件权限；restrict_readonly 是 shell 只读通行的只读环境。纯 ctypes 系统调用，无依赖。aarch64 与 x86_64 的系统调用号相同（444/445/446）。
"""
from __future__ import annotations

import ctypes
import os
import struct

SYS_CREATE, SYS_ADD, SYS_RESTRICT = 444, 445, 446
PR_SET_NO_NEW_PRIVS = 38
CREATE_RULESET_VERSION = 1
RULE_NET_PORT = 2
BIND_TCP, CONNECT_TCP = 1 << 0, 1 << 1
RULE_PATH_BENEATH = 1
FS_EXECUTE, FS_WRITE_FILE, FS_READ_FILE, FS_READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
FS_TRUNCATE, FS_IOCTL_DEV = 1 << 14, 1 << 15
FS_ALL = (1 << 16) - 1                       # ABI 5 起的 16 种文件权限（含 REFER、TRUNCATE、IOCTL_DEV）
FS_FILE = FS_EXECUTE | FS_WRITE_FILE | FS_READ_FILE | FS_TRUNCATE | FS_IOCTL_DEV   # 单个文件（非目录）能给的权限
SCOPE_ABSTRACT_UNIX, SCOPE_SIGNAL = 1 << 0, 1 << 1

_libc = ctypes.CDLL(None, use_errno=True)


def abi_version() -> int:
    r = _libc.syscall(SYS_CREATE, None, 0, CREATE_RULESET_VERSION)
    return int(r) if r >= 0 else 0


def available() -> bool:
    return abi_version() >= 1


def restrict(deny_tcp: bool = False, ports: list[int] | tuple[int, ...] = ()) -> None:
    """在当前进程生效，不可逆：deny_tcp 禁掉 TCP bind / connect（ABI 4 起；不加端口规则 = 全禁），给 network: false 的工具用；
    ports：禁 TCP 时仍放行 connect 到这些端口（Landlock 只认端口不认地址：本机服务的端口，如 ComfyUI 8188、vLLM 8000）。
    ruleset 的 handled_access_fs 为 0（不管文件）。deny_tcp 为 false 时什么都不做（network: true 不限）。"""
    if not deny_tcp:
        return
    abi = abi_version()
    if abi < 1:
        raise OSError("Landlock 不可用")
    if abi < 4:
        raise OSError("Landlock ABI < 4，禁不了 TCP")
    attr = struct.pack("QQQ", 0, BIND_TCP | CONNECT_TCP, 0)     # handled_access_fs = 0, handled_access_net, scoped
    fd = _libc.syscall(SYS_CREATE, attr, 16 if abi < 6 else 24, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    try:
        for port in ports:
            rule = struct.pack("=QQ", CONNECT_TCP, int(port))       # struct landlock_net_port_attr
            if _libc.syscall(SYS_ADD, fd, RULE_NET_PORT, rule, 0) < 0:
                raise OSError(ctypes.get_errno(), f"landlock_add_rule 端口 {port}")
        if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl NO_NEW_PRIVS")
        if _libc.syscall(SYS_RESTRICT, fd, 0) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(fd)


def restrict_readonly(writable: list[str] | tuple[str, ...] = ()) -> None:
    """在当前进程生效，不可逆（shell 只读通行在命令的子进程里用）：全盘可读、可执行；只有 writable 里的路径能写（目录连同下面的）；
    禁 TCP；不能给本进程以外（Landlock 域外）的进程发信号、连域外的抽象 Unix socket。要 ABI ≥ 6，不够抛 OSError。"""
    abi = abi_version()
    if abi < 6:
        raise OSError(f"Landlock ABI {abi} < 6，做不了只读环境")
    fd = _libc.syscall(SYS_CREATE, struct.pack("QQQ", FS_ALL, BIND_TCP | CONNECT_TCP, SCOPE_ABSTRACT_UNIX | SCOPE_SIGNAL), 24, 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")
    try:
        for path, access in (("/", FS_EXECUTE | FS_READ_FILE | FS_READ_DIR | FS_IOCTL_DEV), *((p, 0) for p in writable)):
            pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                access = access or (FS_ALL if os.path.isdir(path) else FS_FILE)
                if _libc.syscall(SYS_ADD, fd, RULE_PATH_BENEATH, struct.pack("=Qi", access, pfd), 0) < 0:   # struct landlock_path_beneath_attr（packed）
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule {path}")
            finally:
                os.close(pfd)
        if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "prctl NO_NEW_PRIVS")
        if _libc.syscall(SYS_RESTRICT, fd, 0) < 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self")
    finally:
        os.close(fd)
