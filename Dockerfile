FROM python:3.12-slim
WORKDIR /app
COPY bot.py .
ENV DB_PATH=/data/study.sqlite3
RUN mkdir /data
CMD ["python", "-u", "bot.py"]
