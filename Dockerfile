# Dockerfile
FROM python:3.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directory for SQLite database
RUN mkdir -p /data
RUN chown -R 1000:1000 /data

# Set environment variables
ENV FLASK_APP=app.py
ENV FLASK_ENV=production
ENV DATABASE_URL=sqlite:////data/database.db
ENV PYTHONUNBUFFERED=1

# Run as non-root user
USER 1000

#CMD ["flask", "run", "--host=0.0.0.0"]
# Use gunicorn instead of Flask's development server
# Single worker so the in-process APScheduler runs exactly once (multiple
# workers = multiple schedulers = duplicated jobs). Threads cover concurrency.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "8", "app:app"]