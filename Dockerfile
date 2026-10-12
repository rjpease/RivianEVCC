FROM python:3.12-slim

WORKDIR /app

COPY rivian_bridge/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy only the application sources. A whole-directory COPY would bake a
# developer's config.json / rivian-session.json into an image layer, where the
# runtime volume mounts would hide them without removing them.
# .dockerignore keeps them out of the build context as well.
COPY rivian_bridge/*.py ./

# config.json and rivian-session.json are mounted via docker-compose volumes
CMD ["python", "-u", "server.py"]
