ARG ARCH=
FROM ${ARCH}python:3.12-alpine

COPY . /app
RUN pip install -r /app/requirements.txt

WORKDIR /app
CMD ["python", "growatt.py"]
