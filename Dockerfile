FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    ASSISTANT_DB=/data/assistant.db \
    HOST=0.0.0.0

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

# Падаем на этапе сборки с понятной причиной, если контекст неполный.
RUN test -f /app/run.py \
    && test -f /app/assistant/api.py \
    && test -f /app/web/index.html \
    && test -f /app/web/sw.js

EXPOSE 8000

# run.py учитывает назначенный Render порт PORT; без ключа API работает rule-диалог.
CMD ["python", "run.py"]
