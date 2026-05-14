FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY wargame_bot.py .

CMD ["python", "wargame_bot.py"]
