FROM python:3.12-slim

WORKDIR /srv
# PYTHONDONTWRITEBYTECODE porque el contenedor arranca con read_only:
# sin esto Python intentaria escribir __pycache__ en un sistema de solo lectura.
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

# Usuario sin privilegios: si alguien escapa por la libreria no oficial, no
# aterriza como root. El UID es fijo para poder alinear el bind mount del host.
RUN useradd --system --uid 10001 --no-create-home garmin \
    && mkdir -p /data \
    && chown -R garmin:garmin /data /srv
USER garmin

VOLUME ["/data"]
EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
