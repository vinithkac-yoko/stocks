FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY server ./server
COPY scripts ./scripts
COPY docs ./docs

# Railway injects PORT; the server reads it. State lives in the volume mounted at /data.
CMD ["python", "server/app.py"]
