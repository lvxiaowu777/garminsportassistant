import paramiko

from deploy_config import HOST, USER, PASS

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=10)

cmds = [
    "sudo systemctl enable nginx 2>&1",
    "sudo systemctl start nginx 2>&1",
    "sleep 1",
    "sudo systemctl status nginx 2>&1 | head -5",
    "echo '=== 80绔彛鐩戝惉 ==='; ss -tlnp | grep ':80 ' || echo '80 not listening'",
    "echo '=== 鏈湴curl娴嬭瘯 ==='; curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://localhost/ 2>&1",
]

for cmd in cmds:
    print(f"$ {cmd[:70]}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=30)
    out = stdout.read().decode().strip()
    err = stderr.read().decode().strip()
    if out:
        print(out)
    if err:
        print(f"ERR: {err}")

ssh.close()
