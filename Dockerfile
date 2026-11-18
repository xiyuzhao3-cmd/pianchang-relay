FROM python:3.12-slim
RUN apt-get update -qq && apt-get install -y -qq ffmpeg && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY server.py .
ENV HOST=0.0.0.0
ENV DATA_DIR=/app/data
CMD ["python3", "server.py"]
