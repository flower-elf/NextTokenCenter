# WorkBuddy Multi-Account Reverse Proxy Gateway
FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1     HOST=0.0.0.0     PORT=8788     API_KEY=     TZ=Asia/Shanghai

WORKDIR /app

RUN apk add --no-cache tzdata ca-certificates &&     cp /usr/share/zoneinfo/${TZ} /etc/localtime &&     echo "${TZ}" > /etc/timezone

# 依赖层放在源码之前：改 app/ 不会让 pip 重装。alpine 用 musllinux wheel，
# cryptography、brotli、zstandard 都有对应版本，构建不需要编译工具链。
COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY app/ ./app/

RUN mkdir -p /app/accounts /app/usage

# Persist credentials and usage logs
VOLUME ["/app/accounts", "/app/usage"]

EXPOSE 8788

# Port probe: /health answers 503 while the account pool is empty, so the
# container check only confirms that the listener is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ.get('PORT', '8788'))), 3).close()"

# --lan listens on every interface and forces an API key (generated once,
# persisted in ./accounts/settings.json and printed in the startup log).
# No --port: the gateway reads PORT itself, and the exec form keeps python as
# PID 1 so `docker stop` delivers SIGTERM.
CMD ["python", "app/wb_proxy.py", "--host", "0.0.0.0", "--lan"]
