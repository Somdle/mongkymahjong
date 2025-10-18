# /dockerfile  (루트)
# Python 3.12 slim
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# 의존성 우선 복사
COPY service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 앱 소스만 복사 (.env 제외)
COPY service/ ./

# 비루트 실행(선택)
RUN useradd -r -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

# 실행
CMD ["python", "app.py"]
