# Only exists because Render's native (non-Docker) Python runtime has no apt
# access, and "Read Player ID" (ocr.py) needs the tesseract-ocr system binary
# - pytesseract is just a wrapper around it, not an OCR engine by itself.
# Everything else about this service is unchanged; deploy it the same way,
# just with the Render service's environment set to Docker instead of Python.
FROM python:3.14-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "main.py"]
