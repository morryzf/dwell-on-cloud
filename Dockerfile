FROM python:3.12-slim

WORKDIR /app

# 先只拷依赖清单再装，这样改代码不会让依赖层缓存失效
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 数据库落在挂载的 volume 里
ENV DWELL_DB=/data/dwell.db
EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
