# Dev image (Windows-friendly) with WeasyPrint runtime libs
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# --- WeasyPrint native dependencies ---
# cairo, pango, gdk-pixbuf, GLib/GObject, HarfBuzz, plus common fonts & XML libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libpangoft2-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libglib2.0-0 \
    libharfbuzz0b \
    libffi-dev \
    libxml2 \
    libxslt1.1 \
    shared-mime-info \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-noto-core \
 && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY . .

# No CMD here — compose supplies the dev command (flask run)
