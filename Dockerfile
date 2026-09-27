FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements-dev.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY bot ./bot
COPY tests ./tests
ENV PYTHONUNBUFFERED=1 TZ=Europe/Moscow
CMD ["python", "-m", "bot.main"]
