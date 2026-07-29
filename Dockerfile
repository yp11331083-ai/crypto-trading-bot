FROM python:3.11-slim

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

# Expose port (Render.com requires this)
EXPOSE 10000

# Health check endpoint - the bot runs a dummy HTTP server on 10000
# so Render.com can detect it as alive
CMD ["sh", "-c", "python -c \"import http.server; s=http.server.HTTPServer(('0.0.0.0',10000), http.server.SimpleHTTPRequestHandler); print('Health check server on :10000')\" & python bot.py"]
