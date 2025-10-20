FROM python:3.11-slim

# optional: smaller image + faster installs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install deps first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app
COPY . .

# Default env paths your bot expects
ENV DB_PATH=/data/levels.db \
    USER_COUNTS_DIR=/data/user_counts

CMD ["python", "-u", "bot.py"]
