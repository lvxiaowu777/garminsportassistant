"""閮ㄧ讲鑴氭湰 鈥斺€?涓婁紶鏂颁唬鐮佸埌鏈嶅姟鍣ㄥ苟閲嶅惎鏈嶅姟"""
import paramiko
import sys
import os

from deploy_config import HOST, USER, PASS
SRC_DIR = os.path.dirname(os.path.abspath(__file__))

files_to_upload = [
    "src/auth.py",
    "src/database.py",
    "src/server.py",
    "src/models.py",
    "src/llm_client.py",
    "src/garmin_sync.py",
    "requirements.txt",
]

commands = [
    # 姣忎釜 exec_command 鏄嫭绔?shell锛宑d 涓嶄繚鎸侊紝鎵€浠ユ瘡鏉￠兘瑕?cd + 鐢?venv/bin 缁濆璺緞
    "cd ~/garmin-coach && venv/bin/pip install PyJWT -q 2>&1 | tail -2; echo 'deps OK'",
    # Kill 鏃ц繘绋?+ 瀛?shell 鍚庡彴鍚姩锛堢粷瀵硅矾寰勶紝褰诲簳 detach锛? 楠岃瘉
    "cd /home/ubuntu/garmin-coach && (pkill -f 'python src/server.py' 2>/dev/null; sleep 1; setsid nohup /home/ubuntu/garmin-coach/venv/bin/python /home/ubuntu/garmin-coach/src/server.py > /home/ubuntu/garmin-coach/logs/server.log 2>&1 < /dev/null &); sleep 4; ps aux | grep 'server.py' | grep -v grep || echo 'WARN server not up'",
    # 楠岃瘉鎺ュ彛
    "curl -s -X POST http://localhost:8766/api/auth/register -H 'Content-Type: application/json' -d '{\"phone\":\"13800000000\",\"password\":\"123456\"}' 2>&1 | head -3",
]

def deploy():
    print(f"Connect {USER}@{HOST}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    try:
        ssh.connect(HOST, username=USER, password=PASS, timeout=10)
        print("[OK] SSH connected")
    except Exception as e:
        print(f"[FAIL] SSH connect failed: {e}")
        return

    sftp = ssh.open_sftp()

    for f in files_to_upload:
        local = os.path.join(SRC_DIR, f)
        remote = f"garmin-coach/{f}"
        try:
            sftp.put(local, remote)
            print(f"  [OK] Upload {f}")
        except Exception as e:
            print(f"  [FAIL] Upload {f}: {e}")

    sftp.close()

    print("\nExecute remote commands...")
    for cmd in commands:
        print(f"  $ {cmd[:60]}{'...' if len(cmd)>60 else ''}")
        stdin, stdout, stderr = ssh.exec_command(cmd, get_pty=True)
        out = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        if out:
            print(f"    {out[:200]}")
        if err:
            print(f"    [ERR] {err[:200]}")

    ssh.close()
    print("\n[OK] Deploy complete!")

if __name__ == "__main__":
    deploy()
