FROM python:3.14-slim

LABEL org.opencontainers.image.title="SolarFlow Smart Energy Management" \
      org.opencontainers.image.description="Schaltet Philips-Hue-Geräte nach dem Solarüberschuss eines Fronius Wechselrichters" \
      org.opencontainers.image.source="https://github.com/JanVogt06/SolarFlow-SmartEnergyManagement" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Zeitzonendaten, damit TZ greift - Statistik, Zeitfenster und Nachttarif rechnen in Ortszeit
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY solarflow ./solarflow
COPY frontend ./frontend

# Alles Veränderliche liegt unter /data: settings.json, devices.json,
# solarflow.db, solar_monitor.log und der Hue-Schlüssel .python_hue.
# HOME zeigt dorthin, damit auch ein alter phue-Schlüssel gefunden wird.
ENV DATA_DIR=/data \
    HOME=/data \
    TZ=Europe/Berlin \
    API_PORT=8000 \
    PYTHONUNBUFFERED=1

VOLUME /data
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5m --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('API_PORT', '8000') + '/api/status').read()"]

CMD ["python", "main.py"]
