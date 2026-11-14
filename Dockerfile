FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Run under a non-root OS user for container security
RUN useradd --create-home appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dinner_agent ./dinner_agent
COPY agent-config.yaml .
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

CMD ["sh", "-c", "adk api_server . --host 0.0.0.0 --port ${PORT}"]
