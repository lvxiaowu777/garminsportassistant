"""
Garmin 首次登录 —— 交互式 MFA，把 token 存到 config/.tokens/，之后 garmin_sync 免密。

只需要跑一次。除非改密码或 token 过期（Garmin 官方 token 有效期约 1 年）。

用法：
    python init_garmin.py
"""

import os
import sys
import getpass
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from garminconnect import Garmin, GarminConnectAuthenticationError

PROJECT_DIR = Path(__file__).resolve().parent.parent
TOKEN_DIR = PROJECT_DIR / "config" / ".tokens"


def main():
    print("═" * 50)
    print("Garmin Connect 首次登录（存 token，之后免密）")
    print("═" * 50)

    email = input("Garmin 邮箱: ").strip()
    if not email:
        print("❌ 邮箱不能空")
        sys.exit(1)
    password = getpass.getpass("密码: ")
    if not password:
        print("❌ 密码不能空")
        sys.exit(1)

    is_cn = input("是不是国内账号？(garmin.cn) [Y/n]: ").strip().lower() != "n"

    TOKEN_DIR.mkdir(parents=True, exist_ok=True)

    def prompt_mfa():
        code = input("手机/邮箱收到的 6 位 MFA 验证码: ").strip()
        return code

    print("\n→ 登录中，可能需要输 MFA...")
    try:
        g = Garmin(email, password, is_cn=is_cn, prompt_mfa=prompt_mfa)
        g.login()
        # 0.3.6 API：token 存在 g.client.dump(path)，不是老版的 g.garth.dump
        g.client.dump(str(TOKEN_DIR))
        # 记住 is_cn，之后 garmin_sync 复用（token 里不带这个标记）
        import json
        (TOKEN_DIR / "config.json").write_text(
            json.dumps({"is_cn": is_cn, "email": email}, ensure_ascii=False),
            encoding="utf-8",
        )

        # 拉一下用户信息确认真登进去了
        fullname = g.get_full_name()
        print(f"\n✅ 登录成功: {fullname}")
        print(f"✅ Token 存到: {TOKEN_DIR}")
        print(f"\n下一步：")
        print(f"  python src/garmin_sync.py            # 拉昨天数据")
        print(f"  python src/daily.py                  # 跑完整日报流程")
    except GarminConnectAuthenticationError as e:
        print(f"\n❌ 认证失败: {e}")
        print("   检查邮箱/密码/is_cn 选项")
        sys.exit(2)
    except Exception as e:
        print(f"\n❌ 出错: {type(e).__name__}: {e}")
        sys.exit(3)


if __name__ == "__main__":
    main()
