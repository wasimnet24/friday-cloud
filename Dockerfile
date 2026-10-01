FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# data dir (notes/memory) container me rahega
RUN mkdir -p data
ENV PORT=8000
EXPOSE 8000
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
