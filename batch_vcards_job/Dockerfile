# Dockerfile
# 使用官方 Python 运行时作为父镜像
FROM python:3.11-slim

# 设置工作目录
WORKDIR /app

# 安装 gcloud CLI 以便更好地与 GCP 环境集成（可选但推荐）
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     curl \
#     gnupg \
#     && echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | tee -a /etc/apt/sources.list.d/google-cloud-sdk.list \
#     && curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | apt-key --keyring /usr/share/keyrings/cloud.google.gpg add - \
#     && apt-get update -y && apt-get install google-cloud-sdk -y \
#     && rm -rf /var/lib/apt/lists/*

# 复制依赖文件并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 定义容器启动时执行的命令
CMD ["python", "main.py"]