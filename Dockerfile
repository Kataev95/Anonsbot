FROM python:3.12-slim
WORKDIR /app
COPY main.py widget.html ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "main.py"]
