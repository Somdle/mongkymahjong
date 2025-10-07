# 베이스 이미지로 Python 3.12 사용
FROM python:3.12-slim

# 작업 디렉토리 설정
WORKDIR /app

# 필요한 패키지 목록 복사
COPY requirements.txt .

# 의존성 설치
RUN pip install --no-cache-dir -r requirements.txt

# 애플리케이션 소스 코드 복사
COPY . .

# 컨테이너 실행 시 실행할 명령어 지정 (예: app.py 실행)
CMD ["python", "app.py"]
