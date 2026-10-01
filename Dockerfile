FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    FACILITY_DB_PATH=/data/facility.db

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 数据目录：挂载卷到这里即可持久化 SQLite
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单 uvicorn worker：进程内的作业线程池与 SQLite 即全部调度状态，
# 多 worker 会各自起一套调度器，反而破坏作业互斥与取消语义。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
