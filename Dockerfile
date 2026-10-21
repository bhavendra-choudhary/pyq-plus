FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir fastapi uvicorn pydantic

COPY pyq_plus_web.py /app/
COPY rpsc_archive.db /app/
COPY static /app/static/

RUN chmod -R 777 /app

EXPOSE 7860

CMD ["uvicorn", "pyq_plus_web:app", "--host", "0.0.0.0", "--port", "7860"]