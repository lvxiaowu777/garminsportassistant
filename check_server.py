import sys
sys.stdout.reconfigure(encoding="utf-8")
import paramiko

from deploy_config import HOST, USER, PASS

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=10)

cmds = [
    "cd ~/garmin-coach/src && /home/ubuntu/garmin-coach/venv/bin/python -c \"from database import SessionLocal; from sqlalchemy import text; s=SessionLocal(); [s.execute(text(f'DELETE FROM {t} WHERE user_id=8')) for t in ['chat_history','watch_bindings','health_daily','activities','daily_plans']]; s.execute(text('DELETE FROM users WHERE id=8')); s.commit(); print('宸插垹 user_id=8 鎵€鏈夋暟鎹?); [print(' ', t, s.execute(text(f'SELECT COUNT(*) FROM {t} WHERE user_id=8')).scalar()) for t in ['chat_history','health_daily','activities','daily_plans']]; s.close()\" 2>&1",
    "ls ~/garmin-coach/logs/ 2>/dev/null || echo 'no logs dir'",
    "tail -50 ~/garmin-coach/logs/server.log 2>/dev/null || echo 'no server log'",
    "ps aux | grep server.py | grep -v grep || echo 'server not running'",
    "ls ~/garmin-coach/src/auth.py ~/garmin-coach/src/database.py ~/garmin-coach/src/server.py 2>&1",
    "cd ~/garmin-coach && source venv/bin/activate && timeout 8 python src/server.py 2>&1 | tail -40",
]

for cmd in cmds:
    print(f"\n=== {cmd} ===")
    stdin, stdout, stderr = ssh.exec_command(cmd)
    for line in stdout:
        print(line.strip())

ssh.close()
