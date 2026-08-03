FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir \
    --trusted-host pypi.org \
    --trusted-host pypi.python.org \
    --trusted-host files.pythonhosted.org \
    -r requirements.txt

# Copy pipeline source
COPY builder.py uploader.py generate.py ./
COPY config/ ./config/

# Seed JSON template — pipeline uses most recent file in resources/
COPY resources/ ./resources/

# Cloud Run Jobs entrypoint
CMD ["python", "generate.py"]
