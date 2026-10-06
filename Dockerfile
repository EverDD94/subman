FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml ./
COPY subman ./subman
RUN pip install --no-cache-dir .
VOLUME ["/config", "/media"]
ENTRYPOINT ["subman"]
CMD ["run", "--watch", "60"]
