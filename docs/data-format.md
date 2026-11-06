# 데이터 형식 v1.0

파일 위치는 `data/laws/<family_id>/<target_language>.json`입니다. UTF-8 JSON만 사용합니다. 템플릿은 `templates/law-family.example.json`이며 실제 법률이 아닌 가상 예시입니다.

| 필드 | 의미 |
| --- | --- |
| `schema_version` | `1.0` 고정 |
| `family_id` | 보존할 법률 묶음 ID; 영문·숫자·`_`·`-` |
| `title_ko` | 한국어 법률명 |
| `source_language` | `ko` |
| `target_language` | 예: `zh-Hans`, `en`, `ja`; 2~3자 소문자 언어 코드와 선택적 하위 태그 |
| `is_example` | 실제 데이터는 `false` |
| `catalog.confirmed` | 전체 카탈로그를 실제 확인했는지 여부 |
| `catalog.expected_document_ids` | 카탈로그에서 확인한 모든 문서 ID |
| `documents` | 문서 목록; ID, 종류, 제목, 공개 출처, 원문 버전, 단위 목록 |
| `documents[].kind` | `act`, `decree`, `rule`, `annex` 중 하나 |
| `units[].unit_id` | 해당 문서 내 고유 조문/별표 단위 ID |
| `units[].source_ko` | 한국어 원문 |
| `units[].translation` | 번역; `pending`/`blocked`일 때 빈 문자열 허용 |
| `units[].status` | `pending`, `reviewed`, `blocked` |
| `units[].reviewed_source_sha256` | 검토 당시 원문 UTF-8 바이트의 SHA-256 |
| `units[].reviewed_by` / `reviewed_at` | 검토자 식별자 / 시간대 포함 ISO 8601 시각 |
| `units[].note` | 교정/보류 사유; `blocked`에서 필수 |
| `review.status` | `in_progress` 또는 `complete` |
| `review.checkpoint_receipt` | 성공 checkpoint 영수증의 공개 가능한 참조 |
| `review.finish_receipt` | 전체 완료 영수증의 공개 가능한 참조 |

`reviewed` 상태는 번역, 검토자, 시간, 일치하는 원문 해시와 checkpoint 참조가 필요합니다. 원문은 줄바꿈과 공백도 해시에 포함됩니다. `pending`/`blocked` 상태에서는 검토자·시각·해시를 빈 문자열로 두어 이전 검수의 오인을 방지합니다.

`complete`는 카탈로그 확인, 문서 ID 목록의 정확한 일치, 모든 단위 `reviewed`, checkpoint 및 finish 영수증 참조가 있어야 합니다. 진행 중에는 finish 참조를 빈 문자열로 둡니다. 확인된 카탈로그와 파일의 문서 목록이 다르면 완료 상태와 무관하게 실패합니다. 카탈로그 미확인 시에도 파일에 있는 문서는 예정 목록에 포함돼야 합니다.

## 자동 검사와 한계

검사기는 중복 JSON 키, 필수 필드/자료형, 허용 상태, 중복 ID, 출처 URL 형식, 해시 불일치, 완료 조건과 실제 데이터 경로를 확인합니다. 실제 데이터 안의 예시 표시와 예약 예시 도메인도 거부합니다.

검사기는 네트워크에 접속하지 않으므로 원출처, 카탈로그, 번역 품질, 검토자의 신원, 영수증 진위를 확인하지 않습니다. 전체 데이터 검사는 `(family_id, target_language)` 중복을 확인합니다. 임의의 외부 파일을 인자로 검사할 때는 파일 구조를 검사하며, 저장소 실제 데이터에 대한 경로 검사는 `data/laws` 내부에서 적용됩니다.

검토한 원문의 해시는 다음처럼 계산할 수 있습니다. 이 계산만으로 검수를 완료한 것은 아닙니다.

```python
import hashlib
digest = hashlib.sha256(source_ko.encode("utf-8")).hexdigest()
```
