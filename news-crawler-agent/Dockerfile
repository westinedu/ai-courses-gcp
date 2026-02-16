FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 复制并安装依赖
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 启用未缓冲输出以便及时看到日志
ENV PYTHONUNBUFFERED=1

# 容器监听端口
EXPOSE 8080

# 默认启动命令
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]