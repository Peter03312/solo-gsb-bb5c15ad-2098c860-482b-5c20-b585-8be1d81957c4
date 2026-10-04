"""容器启动前/独立运行时的数据库初始化入口：python -m app.init_db"""
from .config import DB_PATH
from .db import init_db

if __name__ == "__main__":
    init_db(DB_PATH)
    print(f"数据库已就绪: {DB_PATH}")
