# 删测试力量训练数据（manual_ 开头的 strength_training + 对应 strength_sets）
import re
import sys
import base64
import paramiko
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
src = Path(__file__).with_name("check_server.py").read_text(encoding="utf-8")
HOST = re.search(r'HOST\s*=\s*"([^"]+)"', src).group(1)
USER = re.search(r'USER\s*=\s*"([^"]+)"', src).group(1)
PASS = re.search(r'PASS\s*=\s*"([^"]+)"', src).group(1)

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=20)


def run(cmd, timeout=90):
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    return stdout.read().decode("utf-8", "replace"), stderr.read().decode("utf-8", "replace")


py = '''
import sys
sys.path.insert(0, "/home/ubuntu/garmin-coach/src")
from database import SessionLocal
from models import Activity, StrengthSet
from sqlalchemy import select, delete

s = SessionLocal()
ids = [r[0] for r in s.execute(select(Activity.activity_id).where(
    Activity.user_id == 7, Activity.type == "strength_training",
    Activity.activity_id.like("manual_%")
)).all()]
print("要删的 manual 力量训练:", ids)
if ids:
    s.execute(delete(StrengthSet).where(StrengthSet.activity_id.in_(ids)))
    s.execute(delete(Activity).where(Activity.activity_id.in_(ids)))
    s.commit()
    print("✅ 已删除", len(ids), "条测试力量训练 + 对应动作明细")
else:
    print("无测试数据")
s.close()
'''
b64 = base64.b64encode(py.encode()).decode()
out, err = run(f'cd ~/garmin-coach && source venv/bin/activate && python -c "import base64; exec(base64.b64decode(\'{b64}\'))"')
print(out)
if err.strip():
    print("[stderr]", err[-500:])

ssh.close()
print("[完成]")
