#!/bin/sh
set -e

# If running as root, fix /data directory permissions and structure for Render disk mount
if [ "$(id -u)" = "0" ]; then
    mkdir -p /data/sessions /data/cache /data/config /data/resumes
    chown -R pwuser:pwuser /data /app 2>/dev/null || true
    chmod -R 775 /data 2>/dev/null || true
    exec su -s /bin/sh pwuser -c "exec uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"
else
    mkdir -p /data/sessions /data/cache /data/config /data/resumes 2>/dev/null || true
    exec uvicorn backend.app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 --proxy-headers --forwarded-allow-ips='*'
fi
