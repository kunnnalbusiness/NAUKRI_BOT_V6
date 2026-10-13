# ==============================================================================
# STAGE 1: Build React Frontend
# ==============================================================================
FROM node:22-alpine AS frontend-builder
WORKDIR /app/frontend

COPY frontend/package.json ./
RUN npm install

COPY frontend/ ./
RUN npm run build

# ==============================================================================
# STAGE 2: Playwright Python Production Image
# ==============================================================================
FROM mcr.microsoft.com/playwright/python:v1.49.1-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONIOENCODING=utf-8 \
    PORT=8000 \
    DATA_DIR=/data \
    HEADLESS=true

WORKDIR /app

# Install minimal system dependencies (xvfb for virtual display if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

# Create persistent data directories and set permissions for pwuser
RUN mkdir -p /data/sessions /data/cache /data/config /data/resumes \
    && chown -R pwuser:pwuser /data /app \
    && chmod -R 775 /data

# Install Python backend dependencies
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend and common Python source
COPY --chown=pwuser:pwuser backend/ ./backend/
COPY --chown=pwuser:pwuser common/ ./common/
COPY --chown=pwuser:pwuser *.py ./

# Copy built frontend SPA assets from Stage 1 into /app/frontend/dist
COPY --from=frontend-builder --chown=pwuser:pwuser /app/frontend/dist ./frontend/dist

# Copy entrypoint script
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
