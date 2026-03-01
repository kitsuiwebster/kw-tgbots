FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/bot.py /app/bot.py
COPY app/words.json /app/words.json

CMD ["python", "/app/bot.py"]
