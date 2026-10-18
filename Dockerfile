# Dockerfile
FROM python:3.9-slim

WORKDIR /app

# Install system dependencies + the Infisical CLI.
# The CLI is a pinned .deb from GitHub releases (checksum-verified): Infisical's
# old Cloudsmith apt repo is gone, which broke the apt-based install.
ARG INFISICAL_VERSION=0.43.138
ARG INFISICAL_SHA256_AMD64=9651994ecaf3445770565422c5ca32714f98e110f9e684d0c2aa7dcacbe04b56
ARG INFISICAL_SHA256_ARM64=48a8091bc77eb6d4fa883bacc797dff06401691d6860746a344a1cbf7fec2af2
RUN apt-get update && apt-get install -y \
    sqlite3 \
    bash \
    curl \
    && ARCH="$(dpkg --print-architecture)" \
    && if [ "$ARCH" = "arm64" ]; then SHA="$INFISICAL_SHA256_ARM64"; else SHA="$INFISICAL_SHA256_AMD64"; fi \
    && curl -fsSL -o /tmp/infisical.deb \
       "https://github.com/Infisical/cli/releases/download/v${INFISICAL_VERSION}/infisical_${INFISICAL_VERSION}_linux_${ARCH}.deb" \
    && echo "${SHA}  /tmp/infisical.deb" | sha256sum -c - \
    && apt-get install -y /tmp/infisical.deb \
    && rm -f /tmp/infisical.deb \
    && infisical --version \
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

# Set environment variables.
# FLASK_ENV and DATABASE_URL are intentionally NOT set here — they now come
# solely from Infisical, injected at startup by the entrypoint wrapper. Baking
# defaults here would just be a second source of truth for the same keys.
ENV FLASK_APP=app.py
ENV PYTHONUNBUFFERED=1

# Run as non-root user
USER 1000

#CMD ["flask", "run", "--host=0.0.0.0"]
# Start via the Infisical wrapper: it authenticates with the machine identity,
# pulls this app's secrets from Infisical, and injects them before launching gunicorn.
# gunicorn still runs single-worker (see entrypoint.sh) so APScheduler runs once.
CMD ["/app/entrypoint.sh"]