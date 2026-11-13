FROM python:3.12-slim

WORKDIR /app

# Pre-install CPU-only torch first (no CUDA libs); requirements.txt is
# satisfied by these (torch>=2.0.0) so pip won't re-pull CUDA builds.
# torchvision and torchaudio are not imported anywhere in this repo, so
# they are no longer installed: they roughly doubled the wheel payload
# for nothing.
RUN pip install --no-cache-dir torch \
    --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model's files into the image: the hosted demo runs the
# embedder on the torch free ONNX Runtime backend (embeddings.py) to stay
# under the free tier's 512 MB ceiling, and a pre-warmed HF cache means the
# serving process only loads (never downloads) after each container recycle.
# HF_HOME pins the cache location so the runtime resolves the same files.
ENV HF_HOME=/app/hf
RUN DRIFT_MONITOR_EMBEDDING_BACKEND=onnx python -c \
    "from embeddings import EmbeddingExtractor; EmbeddingExtractor().model" \
    && echo "Torch free ONNX embedder baked into image"

# Select the ONNX backend here rather than through the deployment platform's
# environment: blueprint managed services have been observed to skip newly
# added env vars on sync, and a missed variable means the torch stack loads
# mid cycle and breaches the memory ceiling again. embeddings.py reads this
# and still honors an explicit deployment env var if one is set.
ENV DRIFT_MONITOR_EMBEDDING_BACKEND=onnx

COPY . .

EXPOSE 8000

# Render sets $PORT; default to 8000 for local/docker-compose use.
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
