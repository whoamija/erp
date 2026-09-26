FROM python:3.11-slim

# Prevent Python from writing .pyc files and buffer stdout
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY telegram_erpnext_bot.py .

# Run bot
CMD ["python", "telegram_erpnext_bot.py"]
