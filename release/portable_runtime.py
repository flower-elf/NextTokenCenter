#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布 ZIP 里的便携运行时：一份裁剪过的 CPython 3.12 Windows 运行时（issue #28）。

绿色包在 ``python/`` 下带这份运行时，启动脚本优先使用它，找不到才退回系统
解释器（``start-wb-proxy.bat`` 里是 ``%HERE%python\\python.exe``）。仓库无法
重建它，它被 .gitignore 忽略，本身就是构建输入，所以 CI 从版本固定的
python-build-standalone 下载，再按下面的规则裁剪。

规则取自实测差集：已发布包的 ``python/`` 树是该构建的严格子集，下面几组常量
正好是两者的差集，``Lib/`` 顶层、``DLLs/`` 与根文件都一致。``Lib/multiprocessing``
也在差集里，README 记录了后果（绿色包那份精简 Python 里根本没有这个模块，用它
会直接报错），裁掉它是既定发布行为。

构建自带的 ``Lib/site-packages`` 只装 pip 一类开发组件，整体裁掉；绿色包需要的
cryptography、brotli、zstandard 及其传递依赖由 ``BUNDLED_WHEELS`` 固定，prepare
把它们解进 ``Lib/site-packages``。wheel 与运行时一样按 sha256 校验，发布产物不随
构建机或 PyPI 上的新版本变化。

    python release/portable_runtime.py download --out runtime.tar.gz
    python release/portable_runtime.py wheels  --out wheels
    python release/portable_runtime.py prepare --tarball <tar.gz> --dest python [--wheels wheels]
    python release/portable_runtime.py verify  --root python
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

# ---- 固定的发布输入 ---------------------------------------------------------
#
# 发布资产必须只凭 tag 就能复现：运行时是带校验和的固定下载，与维护者本机装了
# 什么无关。升版本时四个常量一起改；prepare 拒绝 sha256 对不上的 tar 包。
RUNTIME_VERSION = "3.12.15"
RUNTIME_RELEASE = "20261003"
RUNTIME_ASSET = "cpython-3.12.15+20261003-x86_64-pc-windows-msvc-install_only.tar.gz"
RUNTIME_SHA256 = "4b6f0beebbb695a0f3ea237b8c3eaa5bd424f47a7bc25b2fbe3a43390c770f08"
RUNTIME_URL = ("https://github.com/astral-sh/python-build-standalone/releases/download/"
               "%s/%s" % (RUNTIME_RELEASE, RUNTIME_ASSET))

# 绿色包在 Lib/site-packages 下额外携带的 wheel：包名、版本、文件名、sha256、
# 下载地址。运行时是 Windows x64 CPython 3.12，这里只固定这一平台的 wheel；
# cryptography 是 abi3 wheel（cp39 标签），3.12 与更高版本都能用。
#
# 版本要与仓库根的 requirements.txt 一致，tests/_test_release_assets.py 会核对。
# 升级任何一个包时同时改 requirements.txt 与这里的文件名、校验和。
BUNDLED_WHEELS = (
    ("cryptography", "50.0.2",
     "cryptography-50.0.2-cp39-abi3-win_amd64.whl",
     "4e81d95e5bafc2d6e34e4bed780e53e4d5b9a2f928573428aa4d35fbec1eb0de",
     "https://files.pythonhosted.org/packages/f6/b6/a1faf3a27ae9405fb34b1713cc73b2d8a26b04d5c561578fa2e6ef3e5bb9/"
     "cryptography-50.0.2-cp39-abi3-win_amd64.whl"),
    ("cffi", "2.1.1",
     "cffi-2.1.1-cp312-cp312-win_amd64.whl",
     "f53e442b08449d42821fa4a4fba000095af9f62742a500f978a9f557ec44339a",
     "https://files.pythonhosted.org/packages/d9/79/615cc094e2fb508cade7de88d3b4f6c4ec2bab695c97bce9153dc65aadf5/"
     "cffi-2.1.1-cp312-cp312-win_amd64.whl"),
    ("pycparser", "3.12",
     "pycparser-3.12-py3-none-any.whl",
     "a076f3f489b39b43ed13563a1dafe42447445bec2e294154ec5c57fcdda4c85e",
     "https://files.pythonhosted.org/packages/b6/ae/315bbeee126a769cbde43cf4908acb4b40408d8bf8bbf334dd7368d8dadf/"
     "pycparser-3.12-py3-none-any.whl"),
    ("brotli", "1.2.1",
     "brotli-1.2.1-cp312-cp312-win_amd64.whl",
     "f5342b8256476554b4045db8b7bf4dc4044d0405485f220f0cd80764bc0d7faf",
     "https://files.pythonhosted.org/packages/3e/82/b7f6c56393f2224ff4108cf1cc255921be1c0ae5962163edbe367c8a55bc/"
     "brotli-1.2.1-cp312-cp312-win_amd64.whl"),
    ("zstandard", "0.25.0",
     "zstandard-0.25.0-cp312-cp312-win_amd64.whl",
     "ffef5a74088f1e09947aecf91011136665152e0b4b359c42be3373897fb39b01",
     "https://files.pythonhosted.org/packages/79/3b/fa54d9015f945330510cb5d0b0501e8253c127cca7ebe8ba46a965df18c5/"
     "zstandard-0.25.0-cp312-cp312-win_amd64.whl"),
)

# 装完 wheel 后 python/Lib/site-packages 里必须存在的入口文件。入口在，扩展模块
# 才能被 import 找到；verify 逐个核对。
SITE_PACKAGE_FILES = (
    "Lib/site-packages/cryptography/__init__.py",
    "Lib/site-packages/cryptography/hazmat/bindings/_rust.pyd",
    "Lib/site-packages/cffi/__init__.py",
    "Lib/site-packages/_cffi_backend.cp312-win_amd64.pyd",
    "Lib/site-packages/pycparser/__init__.py",
    "Lib/site-packages/brotli.py",
    "Lib/site-packages/_brotli.cp312-win_amd64.pyd",
    "Lib/site-packages/zstandard/__init__.py",
    "Lib/site-packages/zstandard/backend_c.cp312-win_amd64.pyd",
)

# ---- 裁剪规则 ---------------------------------------------------------------

# python.exe 旁边的、绿色包不带的目录：头文件、导入库、tcl/tk 运行时和 pip
# 入口脚本。
DROP_TOP_DIRS = ("Scripts", "include", "libs", "tcl")

# 绿色包不带的标准库包。multiprocessing 是有意裁掉的，见模块 docstring；
# 其余是开发期附加组件。构建自带的 site-packages 只有 pip，整体裁掉后由
# BUNDLED_WHEELS 重新装入，所以这里照样整目录排除。
DROP_LIB_DIRS = ("__pycache__", "ensurepip", "idlelib", "lib2to3", "multiprocessing",
                 "pydoc_data", "site-packages", "tkinter", "turtledemo", "venv")

# 绿色包不带的扩展模块：CPython 测试扩展、tk 绑定及其 tcl/tk DLL，还有
# zlib1.dll。CPython 3.12 把 zlib 链进 python312.dll，这个 DLL 是构建残留。
DROP_DLLS = ("_ctypes_test.pyd", "_testbuffer.pyd", "_testcapi.pyd", "_testclinic.pyd",
             "_testconsole.pyd", "_testimportmultiple.pyd", "_testinternalcapi.pyd",
             "_testmultiphase.pyd", "_testsinglephase.pyd", "_tkinter.pyd",
             "tcl86t.dll", "tk86t.dll", "zlib1.dll")

DROP_ROOT_FILES = ("pythonw.exe",)

# 这些后缀一律裁掉，不论在哪：构建给每个二进制都带调试符号，还有过期的
# 字节码缓存。
DROP_SUFFIXES = (".pdb", ".pyc")

# ---- 绿色包必须具备的能力 ---------------------------------------------------
#
# 启动脚本找 python/python.exe；网关 import 的模块（从 wb_*.py 做 AST 遍历
# 得出）都在这里，裁剪不能带走它们，它们的扩展模块也必须留下。
REQUIRED_FILES = (
    "python.exe", "python3.dll", "python312.dll",
    "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt",
    "Lib/os.py", "Lib/io.py", "Lib/ssl.py", "Lib/hashlib.py", "Lib/hmac.py",
    "Lib/socket.py",
    "Lib/json/__init__.py", "Lib/urllib/__init__.py", "Lib/urllib/request.py",
    "Lib/uuid.py", "Lib/secrets.py",
    "Lib/argparse.py", "Lib/base64.py", "Lib/bisect.py", "Lib/copy.py",
    "Lib/datetime.py",
    "Lib/difflib.py", "Lib/fnmatch.py", "Lib/functools.py", "Lib/getpass.py", "Lib/gzip.py", "Lib/heapq.py",
    "Lib/ipaddress.py", "Lib/queue.py", "Lib/shutil.py", "Lib/random.py",
    "Lib/subprocess.py", "Lib/threading.py", "Lib/sqlite3/__init__.py",
    "Lib/string.py",
    "Lib/html/__init__.py", "Lib/http/__init__.py", "Lib/http/client.py",
    "Lib/email/__init__.py", "Lib/email/utils.py",
    "Lib/concurrent/__init__.py", "Lib/concurrent/futures/__init__.py",
    "Lib/ctypes/__init__.py", "Lib/collections/__init__.py", "Lib/re/__init__.py",
    "DLLs/_socket.pyd", "DLLs/_ssl.pyd", "DLLs/_hashlib.pyd", "DLLs/_ctypes.pyd",
    "DLLs/select.pyd", "DLLs/unicodedata.pyd", "DLLs/_uuid.pyd", "DLLs/_decimal.pyd",
    "DLLs/_sqlite3.pyd",
    "DLLs/libffi-8.dll", "DLLs/libssl-3-x64.dll", "DLLs/libcrypto-3-x64.dll",
)

# 绿色包里没有这些路径，这里也必须保持没有。site-packages 本身要有，但只装
# BUNDLED_WHEELS 那几包，构建自带的 pip / setuptools 不能回来。
FORBIDDEN_PATHS = (
    "Scripts", "include", "libs", "tcl", "pythonw.exe",
    "Lib/multiprocessing", "Lib/tkinter", "Lib/idlelib", "Lib/lib2to3",
    "Lib/ensurepip", "Lib/venv", "Lib/turtledemo",
    "Lib/pydoc_data", "Lib/site-packages/pip", "Lib/site-packages/setuptools",
    "DLLs/_tkinter.pyd", "DLLs/tcl86t.dll", "DLLs/tk86t.dll",
    "DLLs/zlib1.dll",
)


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def keep(rel):
    """固定构建里的这个成员是否该活过裁剪；rel 相对运行时根目录，用正斜杠。"""
    parts = rel.split("/")
    if len(parts) > 1 and parts[0] in DROP_TOP_DIRS:
        return False
    if parts[0] in DROP_ROOT_FILES:
        return False
    if any(p in DROP_LIB_DIRS for p in parts[1:]):
        return False
    if len(parts) == 2 and parts[0] == "DLLs" and parts[1] in DROP_DLLS:
        return False
    return not rel.endswith(DROP_SUFFIXES)


def fetch_wheels(dest):
    """把固定的 wheel 下到 dest，逐个核对 sha256；已存在且正确的文件直接复用。"""
    os.makedirs(dest, exist_ok=True)
    paths = []
    for _name, _version, filename, digest, url in BUNDLED_WHEELS:
        path = os.path.join(dest, filename)
        if not (os.path.isfile(path) and sha256(path) == digest):
            with urllib.request.urlopen(url, timeout=120) as resp:
                with open(path, "wb") as fh:
                    shutil.copyfileobj(resp, fh)
            got = sha256(path)
            if got != digest:
                raise SystemExit("wheel 校验和不符：\n  期望 %s\n  实际 %s\n（%s）"
                                 % (digest, got, path))
        paths.append(path)
    return paths


def install_wheels(root, wheel_dir):
    """把固定的 wheel 解到 root/Lib/site-packages，返回写入的文件数。

    wheel 没有 .data 目录，解压即安装；扩展模块（.pyd）就带在包里。
    """
    target_root = os.path.join(root, "Lib", "site-packages")
    count = 0
    for _name, _version, filename, digest, _url in BUNDLED_WHEELS:
        path = os.path.join(wheel_dir, filename)
        got = sha256(path) if os.path.isfile(path) else ""
        if got != digest:
            raise SystemExit("wheel 缺失或校验和不符：%s（%s）" % (filename, wheel_dir))
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                parts = info.filename.split("/")
                if not parts[0] or any(p in ("", ".", "..") for p in parts):
                    raise SystemExit("wheel 里有非法路径：%s!%s" % (filename, info.filename))
                target = os.path.join(target_root, *parts)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as fh:
                    fh.write(zf.read(info))
                count += 1
    return count


def prepare(tarball, dest, expect_sha=RUNTIME_SHA256, wheel_dir=None):
    """把固定构建解到 dest，裁剪规则要丢的成员直接跳过，再装固定 wheel。

    边读边过滤，被裁掉的约 110 MB 不落盘。wheel_dir 为空时下载到临时目录，
    构建机只多一次网络访问。返回 (kept_files, kept_dirs, wheel_files)。
    """
    got = sha256(tarball)
    if expect_sha and got != expect_sha:
        raise SystemExit("运行时校验和不符：\n  期望 %s\n  实际 %s\n（%s）"
                         % (expect_sha, got, tarball))
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    os.makedirs(dest)

    files = dirs = 0
    with tarfile.open(tarball, "r:gz") as tf:
        for member in tf:
            name = member.name.lstrip("./")
            if not name or name == "python":
                continue
            rel = name[len("python/"):] if name.startswith("python/") else name
            if not rel or not keep(rel):
                continue
            target = os.path.join(dest, *rel.split("/"))
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                dirs += 1
                continue
            if not member.isfile():
                continue  # Windows 构建里没有符号链接
            os.makedirs(os.path.dirname(target), exist_ok=True)
            src = tf.extractfile(member)
            with open(target, "wb") as fh:
                shutil.copyfileobj(src, fh)
            os.chmod(target, 0o755 if rel.endswith(".exe") else 0o644)
            files += 1
    if wheel_dir is None:
        wheel_dir = tempfile.mkdtemp(prefix="wb-wheels-")
        try:
            fetch_wheels(wheel_dir)
            installed = install_wheels(dest, wheel_dir)
        finally:
            shutil.rmtree(wheel_dir, ignore_errors=True)
    else:
        installed = install_wheels(dest, wheel_dir)
    return files, dirs, installed


def verify(root):
    """root 不是启动脚本可用的运行时就抛 SystemExit。"""
    problems = []
    for rel in REQUIRED_FILES + SITE_PACKAGE_FILES:
        if not os.path.isfile(os.path.join(root, *rel.split("/"))):
            problems.append("缺少 %s" % rel)
    for rel in FORBIDDEN_PATHS:
        if os.path.exists(os.path.join(root, *rel.split("/"))):
            problems.append("不该出现 %s（已发布的精简运行时里没有它）" % rel)
    stray = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(DROP_SUFFIXES):
                stray.append(os.path.relpath(os.path.join(dirpath, fn), root))
    if stray:
        problems.append("残留调试符号/字节码：%s%s"
                        % (", ".join(sorted(stray)[:5]),
                           " 等 %d 个" % len(stray) if len(stray) > 5 else ""))
    if problems:
        raise SystemExit("运行时不合格（%s）：\n  - %s" % (root, "\n  - ".join(problems)))
    return len(REQUIRED_FILES) + len(SITE_PACKAGE_FILES)


def _utf8_stdio():
    """把标准输出与标准错误固定为 UTF-8，让进度行在非 UTF-8 控制台下也能打印。

    Windows runner 给 Python 的 stdout 是 cp1252，第一条中文摘要就会抛
    UnicodeEncodeError。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def download(out, expect_sha=RUNTIME_SHA256):
    """下载固定 tar 包，校验和不符就拒绝。"""
    with urllib.request.urlopen(RUNTIME_URL, timeout=120) as resp:
        with open(out, "wb") as fh:
            shutil.copyfileobj(resp, fh)
    got = sha256(out)
    if expect_sha and got != expect_sha:
        raise SystemExit("运行时校验和不符：\n  期望 %s\n  实际 %s\n（%s）"
                         % (expect_sha, got, out))
    return got


def main(argv=None):
    _utf8_stdio()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    d = sub.add_parser("download", help="download the pinned runtime tarball")
    d.add_argument("--out", required=True)
    d.add_argument("--sha256", default=RUNTIME_SHA256,
                   help="expected checksum ('' to skip, for local experiments)")

    p = sub.add_parser("prepare", help="extract + trim the pinned runtime, install pinned wheels")
    p.add_argument("--tarball", required=True)
    p.add_argument("--dest", required=True)
    p.add_argument("--wheels", default=None,
                   help="reuse pinned wheels from this directory instead of downloading")
    p.add_argument("--sha256", default=RUNTIME_SHA256,
                   help="expected checksum ('' to skip, for local experiments)")

    w = sub.add_parser("wheels", help="download the pinned wheels")
    w.add_argument("--out", required=True)

    v = sub.add_parser("verify", help="check a runtime against the launcher contract")
    v.add_argument("--root", required=True)

    args = ap.parse_args(argv)

    if args.cmd == "download":
        download(args.out, args.sha256 or None)
        print("runtime: %s（Python %s）" % (args.out, RUNTIME_VERSION))
        return 0

    if args.cmd == "wheels":
        paths = fetch_wheels(args.out)
        print("wheels: %s（%d 个包）" % (args.out, len(paths)))
        return 0

    if args.cmd == "prepare":
        files, dirs, installed = prepare(args.tarball, args.dest,
                                         args.sha256 or None, args.wheels)
        print("runtime: %s（%d 个文件 / %d 个目录，Python %s）"
              % (args.dest, files, dirs, RUNTIME_VERSION))
        print("wheels: %d 个文件（%s）"
              % (installed, "、".join("%s %s" % (n, v) for n, v, *_ in BUNDLED_WHEELS)))
        verify(args.dest)
        print("runtime: 契约检查通过（%d 个必需文件都在，%d 个已裁项都没回来）"
              % (len(REQUIRED_FILES) + len(SITE_PACKAGE_FILES), len(FORBIDDEN_PATHS)))
        return 0

    if args.cmd == "verify":
        verify(args.root)
        print("runtime ok: %s" % args.root)
        return 0

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
