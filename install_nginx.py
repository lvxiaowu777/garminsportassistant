import paramiko

from deploy_config import HOST, USER, PASS

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=10)

# Install nginx and certbot (with timeout)
cmds = [
    "sudo apt-get update -qq 2>&1 | tail -2",
    "sudo apt-get install -y -qq nginx 2>&1 | tail -3",
    "sudo apt-get install -y -qq certbot python3-certbot-nginx 2>&1 | tail -3",
    "echo '=== nginx version ==='; nginx -v 2>&1",
    "echo '=== certbot version ==='; certbot --version 2>&1",
]

for cmd in cmds:
    print(f"$ {cmd[:70]}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=180)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    if out:
        print(out[-500:])
    if err:
        print(f"ERR: {err[-300:]}")

ssh.close()
print("\nDone")
