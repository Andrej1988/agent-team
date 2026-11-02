# --- frontend (React/Vite) ---------------------------------------------------
FROM node:22-alpine AS web
WORKDIR /w
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# --- backend (FastAPI) --------------------------------------------------------
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ /app/src/
COPY config/ /app/config/
COPY --from=web /w/dist/ /app/web/

RUN useradd -u 1000 -m app
USER app
ENV PYTHONPATH=/app/src PYTHONUNBUFFERED=1 TZ=Europe/Berlin
CMD ["uvicorn", "agent_team.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
