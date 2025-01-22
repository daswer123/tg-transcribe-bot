FROM python:3.10 

WORKDIR /bot 

# Install ffmpeg
RUN apt-get update && apt-get install -y ffmpeg

COPY requirements.txt .

RUN pip install -r requirements.txt

COPY .env .

COPY ./bot .

CMD ["python", "bot.py"]
