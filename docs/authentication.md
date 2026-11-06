# 인증과 자격 증명

## 현재 구성요소

| 구성요소 | 역할 | 자격 증명 처리 |
| --- | --- | --- |
| GitHub 웹 | 파일·PR·설정 관리 | 사용자의 기존 브라우저 로그인 세션 사용 |
| ChatGPT GitHub 연결 | 승인된 저장소 읽기·변경 | 연결 서비스가 관리하는 인증 사용; 저장소에 토큰 저장 안 함 |
| Git / GitHub CLI | 사용자가 선택하는 로컬 개발 경로 | 별도 로컬 인증 필요; 이 초기 설정에서 인증을 생성하지 않음 |
| GitHub Actions | 테스트·데이터 검사 | `contents: read`; checkout 이후 Git 자격 증명 미보존 |
| `scripts/validate.py` | 로컬 JSON 및 상태 검사 | 인증·네트워크·DB 없이 실행 |

브라우저 로그인, ChatGPT GitHub 연결, 로컬 Git 인증은 각각 별도의 접속 경로입니다. 한 경로의 성공만으로 다른 경로의 쓰기 권한을 가정하지 않습니다.

## 요청 흐름

1. 사용자가 GitHub 웹이나 연결 도구에서 변경을 요청합니다.
2. GitHub가 해당 세션/연결의 저장소 권한을 확인하고 커밋 또는 PR을 처리합니다.
3. `main` push 또는 PR 이벤트로 `.github/workflows/validate.yml`이 시작됩니다.
4. Actions가 소스를 가져온 뒤 Python 테스트와 데이터 검사를 실행합니다.
5. 결과가 GitHub 검사 상태로 표시됩니다. 별도의 배포나 운영 DB 쓰기는 없습니다.

## 토큰과 비밀 정보

워크플로는 읽기 전용 `GITHUB_TOKEN` 권한만 선언하며 개인용 PAT나 번역 API 키를 요구하지 않습니다. `actions/checkout`의 `persist-credentials: false`로 인증 정보를 Git 설정에 남기지 않습니다. 작업은 `pull_request_target`을 사용하지 않습니다.

비밀번호·브라우저 쿠키·토큰·DB 비밀번호를 코드, JSON, Issue 또는 로그에 기록하지 않습니다. `.gitignore`는 실수 방지 장치이며 비밀 정보 탐지기나 접근 통제는 아닙니다. 향후 외부 API가 필요하면 그때 필요한 범위의 비밀 설정과 전달 경로를 별도로 설계합니다.

## 아직 구현하지 않은 항목

웹서비스 로그인, OAuth 콜백, 비밀번호 저장, JWT 발급/갱신/폐기, 사용자 DB, 번역 API 및 운영 DB 연결은 없습니다. 따라서 애플리케이션의 토큰 만료 정책도 아직 없습니다. GitHub 계정의 2단계 인증, 저장소 공개 범위, 브랜치 보호 설정은 이 초기 커밋에서 변경하지 않습니다.

참조: [워크플로 구현](../.github/workflows/validate.yml), [검사 스크립트](../scripts/validate.py), [actions/checkout](https://github.com/actions/checkout), [actions/setup-python](https://github.com/actions/setup-python).
