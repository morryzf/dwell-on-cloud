FROM python:3.12-slim

WORKDIR /app

# 先只拷依赖清单再装，这样改代码不会让依赖层缓存失效
# 模型做 PDF 要嵌中文字体
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 数据库落在挂载的 volume 里
ENV DWELL_DB=/data/dwell.db
# 万一要临时下载 PDF 字体，放在 volume 里，重建镜像也不用重下
ENV DWELL_FONT_DIR=/data/fonts
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
