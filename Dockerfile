FROM python:3.12-slim

WORKDIR /app

# Pre-install CPU-only torch stack first (no CUDA libs); requirements.txt
# is satisfied by these (torch>=2.0.0) so pip won't re-pull CUDA builds.
RUN pip install --no-cache-dir torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Render sets $PORT; default to 8000 for local/docker-compose use.
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
