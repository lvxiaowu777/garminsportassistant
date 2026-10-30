"""清空当前会话：DB 里的聊天记录 + （同进程时）内存里的 Agent/CM。

用法：
    python scripts/clear_session.py --user 1001
    python scripts/clear_session.py --user 1001 --date 2026-10-30
    python scripts/clear_session.py --user 1001 --all        # 该用户全部聊天记录
    python scripts/clear_session.py --user 1001 --restart    # 顺便重启 systemd 服务

注意：脚本是**另一个进程**。它能删库，但删不掉正在跑的服务进程里那个 Agent 的 CM。
要让服务里的 CM 也清空，必须重启服务（--restart）或等空闲回收。
"""
from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from garmin_coach.agent.session.store import session_id_for  # noqa: E402
from garmin_coach.db.database import (  # noqa: E402
    clear_chat_history,
    clear_session_chat,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="清空某用户当前会话（DB + 内存 Agent）")
    parser.add_argument("--user", type=int, required=True, help="user_id")
    parser.add_argument("--date", default="", help="会话日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--all", action="store_true", help="清空该用户全部聊天记录")
    parser.add_argument("--restart", action="store_true", help="清完后重启 garmin-coach 服务")
    args = parser.parse_args()

    if args.all:
        deleted = clear_chat_history(args.user)
        print(f"已删除 user={args.user} 的全部聊天记录：{deleted} 行")
    else:
        day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
        sid = session_id_for(args.user, day)
        deleted = clear_session_chat(sid, args.user)
        print(f"已删除会话 {sid}：{deleted} 行")

    # 同进程里如果正好有 Agent，顺手释放（脚本进程通常没有，写了也无害）
    try:
        from garmin_coach.agent import get_agent_pool

        pool = get_agent_pool()
        pool.drop(args.user)
        print(f"已从内存 AgentPool 移除 user={args.user}（仅当前进程）")
    except Exception as exc:  # noqa: BLE001
        print(f"[跳过] 内存 Agent 清理：{exc}")

    if args.restart:
        subprocess.run(["sudo", "-n", "systemctl", "restart", "garmin-coach"], check=False)
        print("已尝试重启 garmin-coach")
    else:
        print("提示：正在跑的服务里还有旧 CM，重启服务才会真正清空：")
        print("      sudo systemctl restart garmin-coach")


if __name__ == "__main__":
    main()
