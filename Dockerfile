FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml subman.example.toml ./
COPY subman ./subman
RUN pip install --no-cache-dir .
VOLUME ["/config", "/media"]
EXPOSE 8787
HEALTHCHECK --interval=2m --timeout=10s --start-period=30s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8787/api/status', timeout=5)"
ENTRYPOINT ["subman"]
CMD ["serve"]
