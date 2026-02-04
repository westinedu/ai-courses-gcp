# 使用Python 3.11官方镜像作为基础（bookworm 更现代，避免 buster 源失效）
FROM python:3.11-slim-bookworm

# 设置工作目录
WORKDIR /app

# 安装系统依赖（ffmpeg用于pydub）。使用非交互模式并最小安装以缩小镜像体积。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates gnupg2 apt-transport-https \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# 复制requirements文件
COPY requirements.txt .

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码和配置
COPY . .

# 暴露端口
EXPOSE 8080

# 启动应用
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
