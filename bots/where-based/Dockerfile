FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/bot.py /app/bot.py

CMD ["python", "/app/bot.py"]
