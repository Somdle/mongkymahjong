## 마장 점수 기록

### Python 버전 관리
- 개발 표준: **Python 3.13 (Eos 2029.10)**  
- 윈도우 환경에서 다중 버전이 설치된 경우, `py` 명령어를 통해 특정 버전을 지정합니다.  
- 기본 버전을 **3.13**으로 고정하려면 다음 중 하나를 선택합니다:

#### (1) 환경변수 방식
```powershell
setx PY_PYTHON "3.13"
setx PY_PYTHON3 "3.13"
py --version
````

#### (2) py.ini 방식 (권장)

```powershell
"[defaults]
python=3.13" | Set-Content -Encoding ASCII "$env:LOCALAPPDATA\py.ini"
```

> 이후 PowerShell 재시작 후 `py --version` 실행 시 3.13이 기본으로 표시됩니다.

---

### MSA (RESTful API가 기본 API)

* **0. Core Service (중앙 관제)**

  * 0.1. 사용자 CRUD
  * 0.2. 점수 CRUD
* **1. AI Service (AI 연산)**
* **2. Discord Chat Service (디스코드 Chat 관리)**
