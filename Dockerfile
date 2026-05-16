ARG ARCH=
FROM ${ARCH}python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY . /app
RUN pip install --no-cache-dir -r /app/requirements.txt

WORKDIR /app
CMD ["python", "growatt.py"]
