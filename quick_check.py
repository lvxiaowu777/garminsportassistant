import paramiko

from deploy_config import HOST, USER, PASS

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=10)

stdin, stdout, stderr = ssh.exec_command("ps aux | grep server.py | grep -v grep")
output = stdout.read().decode().strip()
if output:
    print("RUNNING:", output)
else:
    print("NOT RUNNING")

ssh.close()
