FROM python:3.11-slim

# 不生成 .pyc、日志不缓冲，便于容器排障
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/data/sop.db \
    APP_HOST=0.0.0.0 \
    APP_PORT=8080

WORKDIR /app

# 先装依赖，利用镜像层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码与入口脚本
COPY app ./app
COPY docker-entrypoint.sh ./
RUN chmod +x ./docker-entrypoint.sh

# 持久化 SQLite 数据库（compose 会把命名卷挂到这里）
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# 入口：初始化数据库（幂等）后启动 HTTP 服务
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
