"""部署凭据模板 —— 复制成 .deploy.env 并填入真实值。

    copy deploy_config.example.py .deploy.env   # 然后按下面的格式改成 key=value
"""
# .deploy.env 的真实格式（不是 Python 语法）：
#
# GARMIN_HOST=你的服务器IP
# GARMIN_USER=ubuntu
# GARMIN_PASS=你的服务器密码
#
# 也可以改用环境变量（同名），优先级高于 .deploy.env。
