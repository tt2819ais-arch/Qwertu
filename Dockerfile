FROM python:3.11-slim

WORKDIR /app

# Копируем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY bot.py .

# Создаем папку для медиа
RUN mkdir -p saved_media/photos

# Создаем пользователя
RUN useradd -m -u 1000 botuser && chown -R botuser:botuser /app
USER botuser

# Запуск
CMD ["python", "bot.py"]
