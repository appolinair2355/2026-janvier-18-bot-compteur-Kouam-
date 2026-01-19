FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Render needs to bind to 0.0.0.0
ENV PORT=10000
EXPOSE 10000

CMD ["python", "main.py"]
