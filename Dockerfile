FROM python:3.11-slim

# System packages for WeasyPrint and other tools
RUN apt-get update && apt-get install -y \
    build-essential \
    libffi-dev \
    libssl-dev \
    libjpeg-dev \
    libpq-dev \
    libpangocairo-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libxml2 \
    libxslt1.1 \
    libfontconfig1 \
    libglib2.0-0 \
    && apt-get clean

# Create app directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY . .
COPY app/static /app/static

# Set environment variables
ENV FLASK_ENV=development
ENV FLASK_DEBUG=1
ENV PYTHONUNBUFFERED=1

# Run the Flask app

CMD ["flask", "run", "--host=0.0.0.0"]
