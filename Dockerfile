FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

RUN groupadd --gid 10001 agentgroup \
    && useradd --uid 10001 --gid agentgroup --create-home --shell /usr/sbin/nologin agentuser

COPY requirements.txt pyproject.toml README.md ./
COPY oura_connect ./oura_connect
COPY evals ./evals

RUN pip install --no-cache-dir -r requirements.txt

USER agentuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"

CMD ["python3", "-m", "oura_connect", "serve", "--host", "0.0.0.0", "--port", "8080"]
