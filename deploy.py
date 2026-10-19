"""部署脚本 —— 打包上传、备份、重启、验证。

用法：
    python deploy.py --check     # 只检查服务器环境，不改任何东西
    python deploy.py             # 执行部署

设计要点：
- **整目录打包上传**，不再维护一份容易过时的文件清单（旧脚本漏了
  garmin_coach/ 和 static/，因为清单是手写的）。
- **不覆盖服务器的运行时数据**：.env / config/.tokens / src/data / logs /
  static/audio 全部排除，这些是服务器上独有的。
- **先备份再覆盖**：服务器上 src/ 会复制成 src.bak.<时间戳>，出问题可回滚。
- **前置检查环境变量**：database.py 现在要求 DATABASE_URL 必须存在，
  缺了服务起不来。检查不通过就中止，不冒险重启。

凭据来自 .deploy.env（gitignored）或环境变量，见 deploy_config.py。
"""
from __future__ import annotations

import os
import sys
import time
import tarfile

import paramiko

from deploy_config import require_credentials

SRC_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE_DIR = "garmin-coach"

# 要上传的目录和文件（相对项目根）
INCLUDE_DIRS = ("src", "garmin_coach", "static")
INCLUDE_FILES = ("requirements.txt",)

# 不打包：这些是本地或服务器上各自独有的东西
EXCLUDE_DIR_PARTS = {"__pycache__", ".git", "venv", ".venv", "node_modules", "audio"}
EXCLUDE_FILES = {".env", ".deploy.env", ".deploy.env.example", ".gitignore"}
EXCLUDE_SUFFIXES = (".db", ".db.bak", ".db-shm", ".db-wal")


def build_archive(dest: str) -> tuple[int, list[str]]:
    """把需要部署的文件打成 tar.gz，返回 (文件数, 相对路径列表)。"""
    count = 0
    names: list[str] = []
    with tarfile.open(dest, "w:gz") as tar:
        for d in INCLUDE_DIRS:
            base = os.path.join(SRC_DIR, d)
            if not os.path.isdir(base):
                continue
            for root, dirs, files in os.walk(base):
                dirs[:] = [x for x in dirs if x not in EXCLUDE_DIR_PARTS]
                for f in files:
                    if f in EXCLUDE_FILES or f.endswith(EXCLUDE_SUFFIXES):
                        continue
                    full = os.path.join(root, f)
                    rel = os.path.relpath(full, SRC_DIR).replace(os.sep, "/")
                    if any(part in EXCLUDE_DIR_PARTS for part in rel.split("/")):
                        continue
                    tar.add(full, arcname=rel)
                    names.append(rel)
                    count += 1
        for f in INCLUDE_FILES:
            full = os.path.join(SRC_DIR, f)
            if os.path.isfile(full):
                tar.add(full, arcname=f)
                names.append(f)
                count += 1
    return count, names


def connect():
    host, user, password = require_credentials()
    print(f"连接 {user}@{host} …")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=password, timeout=15)
    print("[OK] SSH 已连接")
    return ssh


def run(ssh, cmd: str, timeout: int = 120) -> tuple[int, str]:
    """执行一条远程命令，返回 (退出码, 合并输出)。

    注意不用 get_pty：带 pty 时 SSH 通道关闭会把后台子进程一起带走，
    导致 `nohup ... &` 起来的服务活不过这次连接。
    """
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    return code, (out + err).strip()


def start_service(ssh) -> bool:
    """停旧进程并后台启动，返回是否确认在跑。

    两个踩过的坑，这里都规避了：

    1) `pkill -f 'server.py'` 会匹配到**执行它的那条 shell 命令**（因为命令行里
       就含这个字符串），把自己杀掉，后面的启动指令根本执行不到。
       用 `server[.]py` 让 pattern 字面量不再等于目标字符串。

    2) 启动后如果 `stdout.read()`，会一直等 EOF —— 子进程继承了 stdout 管道，
       只要服务活着管道就不关，于是挂到超时。改用 readline 只读那一行确认。
    """
    run(ssh, "pkill -f 'server[.]py' 2>/dev/null; sleep 2; echo KILLED")

    start = (
        f"cd ~/{REMOTE_DIR} && "
        "nohup setsid venv/bin/python src/server.py >> logs/server.log 2>&1 < /dev/null & "
        "disown; echo LAUNCHED"
    )
    _in, out, _err = ssh.exec_command(start, timeout=30)
    try:
        out.channel.settimeout(10)
        print(f"  {out.readline().strip() or 'LAUNCHED'}")
    except Exception:
        print("  （启动指令已发出）")

    print("  等待服务就绪…")
    time.sleep(8)
    _code, count = run(ssh, "ps aux | grep 'server[.]py' | grep -v grep | wc -l")
    running = count.strip().isdigit() and int(count.strip()) > 0
    print(f"  进程数: {count.strip()}")
    return running


ENV_CHECK = (
    f"cd ~/{REMOTE_DIR} && "
    "test -d venv && echo 'VENV_OK' || echo 'VENV_MISSING'; "
    "test -f .env && echo 'ENVFILE_OK' || echo 'ENVFILE_MISSING'; "
    "grep -q '^DATABASE_URL=' .env 2>/dev/null && echo 'DBURL_OK' || echo 'DBURL_MISSING'; "
    "venv/bin/python -V 2>&1"
)


def check(ssh) -> bool:
    """只检查，不改动。返回环境是否可部署。"""
    print("\n── 服务器环境检查 ──")
    code, out = run(ssh, ENV_CHECK)
    print(out)
    ok = "DBURL_OK" in out and "VENV_OK" in out
    if "DBURL_MISSING" in out:
        print(
            "\n[中止] 服务器 .env 缺少 DATABASE_URL。\n"
            "database.py 现在要求它必须存在（不再内置 MySQL 默认值）。\n"
            "请在服务器上补一行，例如：\n"
            "  DATABASE_URL=mysql+pymysql://用户:密码@localhost:3306/garmin_coach?charset=utf8mb4"
        )
    if "VENV_MISSING" in out:
        print("\n[中止] 服务器上没有 venv/，先按部署文档建虚拟环境。")
    return ok


def deploy(ssh) -> bool:
    if not check(ssh):
        return False

    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = os.path.join(SRC_DIR, f"deploy-{stamp}.tar.gz")

    print("\n── 打包 ──")
    count, names = build_archive(archive)
    size_mb = os.path.getsize(archive) / 1024 / 1024
    print(f"  {count} 个文件，{size_mb:.2f} MB")
    for n in names:
        if n.startswith(("garmin_coach/", "static/")):
            print(f"    + {n}")

    remote_archive = f"/home/ubuntu/deploy-{stamp}.tar.gz"

    print("\n── 上传 ──")
    sftp = ssh.open_sftp()
    try:
        sftp.put(archive, remote_archive)
        print(f"  [OK] {remote_archive}")
    finally:
        sftp.close()

    print("\n── 备份现有 src/ ──")
    code, out = run(ssh, f"cd ~/{REMOTE_DIR} && cp -r src src.bak.{stamp} && echo BACKUP_OK")
    print(f"  {out}")
    if "BACKUP_OK" not in out:
        print("[中止] 备份失败，不继续（避免无法回滚）")
        return False

    print("\n── 解压覆盖 ──")
    code, out = run(ssh, f"cd ~/{REMOTE_DIR} && tar -xzf {remote_archive} && echo EXTRACT_OK")
    print(f"  {out}")
    if "EXTRACT_OK" not in out:
        print("[中止] 解压失败")
        return False

    print("\n── 安装依赖 ──")
    code, out = run(
        ssh,
        f"cd ~/{REMOTE_DIR} && venv/bin/pip install -q -r requirements.txt 2>&1 | tail -3; echo DEPS_DONE",
        timeout=300,
    )
    print(f"  {out[:600]}")

    print("\n── 重启服务 ──")
    started = start_service(ssh)
    if not started:
        print("[WARN] 进程没起来，日志尾部：")
        _c, tail = run(ssh, f"tail -25 ~/{REMOTE_DIR}/logs/server.log")
        print(tail)

    print("\n── 验证 ──")
    checks = [
        ("首页 HTML", "curl -s -o /dev/null -w '%{http_code} %{size_download}' http://localhost:8766/"),
        ("LLM providers", "curl -s -o /dev/null -w '%{http_code}' http://localhost:8766/api/llm/providers"),
        ("OpenAPI", "curl -s -o /dev/null -w '%{http_code}' http://localhost:8766/openapi.json"),
        ("日志尾部", f"tail -6 /home/ubuntu/{REMOTE_DIR}/logs/server.log"),
    ]
    all_ok = True
    for label, cmd in checks:
        code, out = run(ssh, cmd)
        print(f"  {label}: {out[:400]}")
        if label != "日志尾部" and not out.startswith("200"):
            all_ok = False

    print(f"\n{'[OK] 部署完成' if all_ok else '[WARN] 部署完成但验证未全部通过，检查上面的日志'}")
    print(f"回滚命令：cd ~/{REMOTE_DIR} && rm -rf src && mv src.bak.{stamp} src && <重启>")

    os.remove(archive)
    run(ssh, f"rm -f {remote_archive}")
    return all_ok


def main() -> int:
    only_check = "--check" in sys.argv
    ssh = connect()
    try:
        if only_check:
            ok = check(ssh)
            print("\n环境可部署" if ok else "\n环境有问题，先修")
            return 0 if ok else 1
        return 0 if deploy(ssh) else 1
    finally:
        ssh.close()


if __name__ == "__main__":
    sys.exit(main())
