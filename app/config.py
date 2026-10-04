"""运行期配置，全部来自环境变量，均带默认值。"""
import os

# SQLite 数据库文件路径（容器内默认放在挂载卷 /data 下）
DB_PATH = os.environ.get("DB_PATH", "/data/sop.db")

# HTTP 监听地址与端口
APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "8080"))

# 写事务等待锁的超时时间（秒）
DB_TIMEOUT = float(os.environ.get("DB_TIMEOUT", "10"))
