import os

import paramiko

from deploy_config import HOST, USER, PASS

DOMAIN = os.environ.get("GARMIN_DOMAIN", "example.com")

nginx_conf = f"""server {{
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name {DOMAIN};

    ssl_certificate /etc/letsencrypt/live/{DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{DOMAIN}/privkey.pem;

    location / {{
        proxy_pass http://127.0.0.1:8766;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # WebSocket 支持（聊天功能需要）
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }}
}}

server {{
    listen 80;
    listen [::]:80;
    server_name {DOMAIN};
    return 301 https://$host$request_uri;
}}
"""

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(HOST, username=USER, password=PASS, timeout=10)

# 写入新配置
print("Writing nginx config...")
sftp = ssh.open_sftp()
with sftp.open("/tmp/garmin-coach-nginx.conf", "w") as f:
    f.write(nginx_conf)
sftp.close()

cmds = [
    "sudo cp /tmp/garmin-coach-nginx.conf /etc/nginx/sites-available/garmin-coach",
    "sudo ln -sf /etc/nginx/sites-available/garmin-coach /etc/nginx/sites-enabled/garmin-coach",
    "sudo rm -f /etc/nginx/sites-enabled/default",
    "sudo nginx -t 2>&1",
    "sudo systemctl reload nginx 2>&1",
    "sleep 1",
    "echo '=== 本地测试 ==='; curl -s -o /dev/null -w 'HTTP %{http_code}\n' http://localhost/ 2>&1",
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
print("\nDone")
