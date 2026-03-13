FROM python:3.12-slim

WORKDIR /app

COPY rivian_bridge/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY rivian_bridge/ ./

# config.json and rivian-session.json are mounted via docker-compose volumes
CMD ["python", "-u", "server.py"]
