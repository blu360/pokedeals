FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    POKEDEALS_DB=/data/pokedeals.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pokedeals ./pokedeals
COPY data/sample_listings.json ./data/sample_listings.json
RUN useradd --create-home --uid 1000 pokedeals && mkdir /data && chown pokedeals /data
USER pokedeals
VOLUME /data
EXPOSE 8765

# "ready" once the card catalogue has been downloaded (the first start takes a few minutes)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8765/api/setup', timeout=4)" || exit 1

# EBAY_CLIENT_ID / EBAY_CLIENT_SECRET (or ..._FILE for Docker secrets) come from the environment
CMD ["python", "-m", "pokedeals", "start", "--host", "0.0.0.0", "--no-browser"]
