FROM python:3.11-slim

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

ENV COLDCHAIN_DB=/data/coldchain.db
VOLUME /data
EXPOSE 8000

# 单 worker：SQLite 单文件库，写操作串行即可；读查询各自独立事务
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
