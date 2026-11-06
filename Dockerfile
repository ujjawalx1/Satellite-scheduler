FROM python:3.11-slim
WORKDIR /code
ENV PYTHONPATH=/code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY scripts ./scripts
COPY tests ./tests
COPY pytest.ini .
COPY locustfile.py .
CMD ["uvicorn","app.core:app","--host","0.0.0.0","--port","8000"]
