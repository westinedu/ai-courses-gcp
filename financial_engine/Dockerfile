# 使用一个轻量级的 Python 官方镜像作为基础
# python:3.11-slim-buster 是一个很好的选择，它包含了运行 Python 应用所需的一切，但移除了不必要的开发工具和库，以减小镜像大小。
FROM python:3.11-slim-buster

# 设置工作目录
# 容器内的所有后续命令都将在这个目录下执行
WORKDIR /app

# 复制 requirements.txt 文件到工作目录
# 这一步单独进行是为了利用 Docker 的构建缓存。如果 requirements.txt 没有变化，
# 那么 pip install 这一层将不会重新执行，加快后续构建速度。
COPY requirements.txt .

# 安装 Python 依赖
# --no-cache-dir 选项可以防止 pip 缓存下载的包，进一步减小镜像大小
# uvicorn[standard] 已经包含了 gunicorn，但如果仅部署在 Cloud Run，可以直接用 uvicorn
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用程序的所有代码到工作目录
# 包括 main.py 和任何其他依赖的文件（例如 data 目录会在 main.py 启动时创建）
COPY . .

# 暴露应用程序监听的端口
# Cloud Run 会自动识别并使用这个端口，但明确指定它是个好习惯。
# 您的 main.py 中 Uvicorn 配置为 8080
EXPOSE 8080

# 定义容器启动时执行的命令
# 使用 uvicorn 启动 FastAPI 应用。
# --host 0.0.0.0 是必须的，这样 Uvicorn 才能在容器内部监听所有网络接口。
# --port 8080 与 EXPOSE 保持一致。
# --workers 1 (或根据需要调整) 可以控制 Uvicorn 的工作进程数量。对于 Cloud Run，通常单个实例就足够，并且 Cloud Run 会自动处理并发。
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]