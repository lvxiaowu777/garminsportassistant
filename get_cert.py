import os

import paramiko

from deploy_config import HOST, USER, PASS

DOMAIN = os.environ.get("GARMIN_DOMAIN", "example.com")

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=10)

cmd = (
    f"sudo certbot --nginx -d {DOMAIN} "
    "--non-interactive --agree-tos "
    f"--email admin@{DOMAIN} "
    "--redirect 2>&1"
)

print(f"$ {cmd}")
stdin, stdout, stderr = ssh.exec_command(cmd, timeout=120)
out = stdout.read().decode().strip()
err = stderr.read().decode().strip()
if out:
    print(out)
if err:
    print(f"ERR: {err}")

# 检查证书是否签发成功
print("\n=== 证书检查 ===")
stdin, stdout, stderr = ssh.exec_command(f"sudo certbot certificates 2>&1")
for line in stdout:
    print(line.strip())

ssh.close()
