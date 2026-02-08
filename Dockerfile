FROM python:3.11-slim

# Instalar FFmpeg
RUN apt-get update \
  && apt-get install -y --no-install-recommends ffmpeg \
  && rm -rf /var/lib/apt/lists/*

# Directorio de trabajo
WORKDIR /app

# Instalar dependencias
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copiar código
COPY . /app

# Render expone el puerto en $PORT
ENV PORT=10000
EXPOSE 10000

CMD ["sh", "-c", "uvicorn app:app --app-dir /app --host 0.0.0.0 --port ${PORT}"]
