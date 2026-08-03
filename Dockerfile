# Dockerfile
FROM python:3.9-slim

WORKDIR /app

# Install system dependencies + the Infisical CLI
RUN apt-get update && apt-get install -y \
    sqlite3 \
    bash \
    curl \
    && curl -1sLf 'https://dl.cloudsmith.io/public/infisical/infisical-cli/setup.deb.sh' | bash \
    && apt-get update && apt-get install -y infisical \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Make the Infisical startup wrapper executable
RUN chmod +x /app/entrypoint.sh

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
# Start via the Infisical wrapper: it authenticates with the machine identity,
# pulls this app's secrets from Infisical, and injects them before launching gunicorn.
# gunicorn still runs single-worker (see entrypoint.sh) so APScheduler runs once.
CMD ["/app/entrypoint.sh"]