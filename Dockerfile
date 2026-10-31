FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ASSISTANT_DB=/data/assistant.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Без ключа api работает офлайн rule-диалог.
CMD ["uvicorn", "assistant.api:app", "--host", "0.0.0.0", "--port", "8000"]
