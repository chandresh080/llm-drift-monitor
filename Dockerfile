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

# Bake the embedding model's ONNX export into the image: the hosted demo
# runs the embedder on the ONNX Runtime backend to stay under the free
# tier's 512 MB ceiling, and a pre-built graph means the serving process
# only loads (never exports) after each container recycle. HF_HOME pins
# the cache location so the runtime resolves the same export.
ENV HF_HOME=/app/hf
RUN python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2', device='cpu', backend='onnx')" \
    && echo "ONNX embedding export baked into image"

COPY . .

EXPOSE 8000

# Render sets $PORT; default to 8000 for local/docker-compose use.
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000}"]
