# LAWS OF KOREA

한국 법률 원문과 다국어 번역을 대조하고, 법률 묶음 단위로 검수 이력을 관리하는 저장소입니다. 초기 작업 언어는 한국어 → 중국어 간체(`zh-Hans`)이며, 같은 형식으로 다른 언어를 추가할 수 있습니다.

## 현재 구성

- 법률·시행령·시행규칙·별표/서식을 하나의 법률 묶음으로 관리합니다.
- 원문 식별자와 버전을 보존하고, 실제로 검토한 항목만 검수 완료로 기록합니다.
- JSON 형식, 원문 해시, 검수 상태와 완료 조건을 자동 검사합니다.
- 실제 법률 데이터, 번역 서비스, 운영 DB 연결, 웹 로그인 기능은 아직 포함하지 않습니다.

## 시작하기

Python 3.11 이상이 필요하며 외부 Python 패키지는 필요하지 않습니다.

```bash
git clone https://github.com/anbin19000-design/lawsofkorea.git
cd lawsofkorea
python -m unittest discover -s tests -v
python scripts/validate.py
```

1. `templates/law-family.example.json`을 참고해 `data/laws/<family_id>/<language>.json`을 만듭니다. 예시 문구는 실제 법률이 아닙니다.
2. 실제 원문, 출처, 버전과 중앙 카탈로그의 문서 목록을 채우고 `is_example`을 `false`로 변경합니다.
3. [검수 절차](docs/workflow.md)에 따라 실제 검토 범위와 저장 영수증을 기록합니다.
4. 검사를 실행한 뒤 변경을 별도 브랜치와 Pull Request로 제출합니다.

## 안내

| 위치 | 내용 |
| --- | --- |
| [docs/workflow.md](docs/workflow.md) | 전체 법률 검수와 저장/완료 처리 |
| [docs/data-format.md](docs/data-format.md) | 데이터 필드와 검사 범위 |
| [docs/authentication.md](docs/authentication.md) | GitHub 인증, 요청 흐름, 토큰 처리 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 기여·브랜치·검사 방법 |
| [AGENTS.md](AGENTS.md) | 자동화 작업자 지침 |
| [data/laws](data/laws) | 공개 가능한 법률별 데이터 |

CI의 `validate` 작업은 데이터 구조를 확인합니다. 법률적 정확성, 번역 품질, 출처의 최신성, 영수증 진위, 운영 DB 반영 여부를 보증하지 않습니다. 이 저장소의 번역을 공식 번역으로 표시하지 않습니다.

현재 저장소는 공개 상태입니다. 공개가 허용된 자료만 추가하고, 비밀번호·토큰·DB 덤프·브라우저 프로필·내부 원장을 커밋하지 않습니다. 기존 자료의 이용 조건은 원출처를 따르며, 별도의 배포 라이선스는 아직 지정하지 않았습니다.
