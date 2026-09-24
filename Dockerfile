FROM python:3.11-slim

# Prevent Python from writing .pyc files & enable unbuffered logging
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860

# Set up working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt


# Copy application files
COPY . /app

# Create a non-root user (Hugging Face Spaces runs as user 1000)
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app
USER appuser

# Expose Hugging Face Space default port 7860
EXPOSE 7860

# Run Uvicorn on 0.0.0.0:7860
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
