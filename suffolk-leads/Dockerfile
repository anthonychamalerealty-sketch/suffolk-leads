FROM python:3.11-slim-buster

# Force Python to flush stdout/stderr immediately â critical for Railway logs.
# Without this, print() output may be buffered and never appear in Railway logs
# before the container exits.
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install Python dependencies first (layer-cached)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright Chromium browser (graceful fallback so build never fails)
RUN playwright install chromium --with-deps || \
    echo "WARNING: playwright install failed â browser scraping will be skipped"
RUN playwright install chromium
RUN playwright install-deps chromium

# Copy all source code
COPY . .

# Default entry point: main.py
# The cron Railway service overrides this with:
#   startCommand = "python jobs/run_all.py"   (railway-cron.toml)
CMD ["python", "main.py"]
