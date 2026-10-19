import paramiko, time

from deploy_config import HOST, USER, PASS

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=10)

# Start server with nohup
print("Starting server...")
transport = ssh.get_transport()
channel = transport.open_session()
channel.exec_command(
    "mkdir -p ~/garmin-coach/logs && "
    "cd ~/garmin-coach && "
    "source venv/bin/activate && "
    "nohup python src/server.py > logs/server.log 2>&1 &"
)
channel.close()

time.sleep(4)

# Check status
print("\nProcess check:")
stdin, stdout, stderr = ssh.exec_command("ps aux | grep server.py | grep -v grep")
for line in stdout:
    print(line.strip())

print("\nLog output:")
stdin, stdout, stderr = ssh.exec_command("cat ~/garmin-coach/logs/server.log 2>/dev/null || echo 'empty log'")
for line in stdout:
    print(line.strip())

# Test endpoint
print("\nTest endpoint:")
stdin, stdout, stderr = ssh.exec_command(
    "curl -s -m 3 -X POST http://localhost:8766/api/auth/register "
    "-H 'Content-Type: application/json' "
    "-d '{\"phone\":\"13800000000\",\"password\":\"123456\"}' 2>&1"
)
for line in stdout:
    print(line.strip())

ssh.close()
