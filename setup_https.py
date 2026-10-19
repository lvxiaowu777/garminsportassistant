import paramiko

from deploy_config import HOST, USER, PASS

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=10)

cmds = [
    "echo '=== OS ==='; cat /etc/os-release | head -2",
    "echo '=== nginx ==='; which nginx || echo 'not installed'",
    "echo '=== certbot ==='; which certbot || echo 'not installed'",
    "echo '=== python3 ==='; python3 --version",
    "echo '=== 80绔彛鐩戝惉 ==='; ss -tlnp | grep ':80 ' || echo '80 not listening'",
    "echo '=== 443绔彛鐩戝惉 ==='; ss -tlnp | grep ':443 ' || echo '443 not listening'",
    "echo '=== 8766绔彛鐩戝惉 ==='; ss -tlnp | grep ':8766 ' || echo '8766 not listening'",
    "echo '=== ufw鐘舵€?==='; sudo ufw status 2>/dev/null || echo 'ufw not active or not installed'",
    "echo '=== iptables ==='; sudo iptables -L -n 2>/dev/null | head -20 || echo 'no iptables access'",
]

for cmd in cmds:
    stdin, stdout, stderr = ssh.exec_command(cmd)
    out = stdout.read().decode().strip()
    if out:
        print(out)
    print()

ssh.close()
