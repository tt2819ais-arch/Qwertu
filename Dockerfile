FROM python:3.11-slim

WORKDIR /app

# Установка минимальных зависимостей
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Сначала копируем requirements для кэширования
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Копируем основной код
COPY bot.py .

# Создаем не-root пользователя
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

# Переменные окружения
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Команда запуска
CMD ["python", "bot.py"]
