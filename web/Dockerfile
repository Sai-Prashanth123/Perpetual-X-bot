# Agency dashboard + X connect + the post endpoint the Slack /post command calls.
FROM python:3.12-slim

WORKDIR /app
COPY web/requirements.txt web/requirements.txt
RUN pip install --no-cache-dir -r web/requirements.txt
COPY lib/ lib/
COPY web/ web/

ENV PYTHONPATH=/app/lib PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app:app --app-dir web --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
