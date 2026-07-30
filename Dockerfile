FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy pipeline source
COPY builder.py uploader.py generate.py ./
COPY config/ ./config/

# Optional: place a seed JSON template in resources/
# COPY resources/ ./resources/

# Cloud Run Jobs entrypoint
CMD ["python", "generate.py"]
