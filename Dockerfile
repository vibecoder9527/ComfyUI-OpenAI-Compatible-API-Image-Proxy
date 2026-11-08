FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app-en.py ./app.py
COPY workflow ./workflow

EXPOSE 8000

CMD ["uvicorn", "app:app", "--forwarded-allow-ips", "*", "--host", "0.0.0.0", "--port", "8000"]