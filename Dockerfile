FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /srv/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY eval.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /srv/app /data

ENV DATA_DIR=/data
EXPOSE 8080

# Starts as root only to chown the mounted volume, then execs as appuser.
ENTRYPOINT ["/srv/app/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
