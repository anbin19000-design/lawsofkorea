# 법령가족 공급기 V2 — 관측기 연동

관측기는 별도 대화 `6aee54fb-1a94-83ee-ab44-7ac18c2f5592`가 담당한다. 관측기는 10초 주기로 6개 패널을 읽는다. 공급기는 별도의 일반 사용자 상주 프로세스로 10초 주기 실행하며 관측 결과를 받아 유휴 슬롯 전체를 일괄 처리한다. 발송 중에는 해당 전송의 성공 확인을 위한 제한된 panel.observe를 수행한다.

| PC | 저장소 | 작업 브랜치 | 워커 |
|---|---|---|---|
| WORK-C | anbin19000-design/lawsofkorea | main | C01–C06 |
| NOTEX | anbin1900-crypto/yolla-gas-station-portal | law-review-notex | N01–N06 |

두 manifest는 각각 785/813개 가족이며 설치 시 ID 교집합은 0이었다. 설정의 `partition_manifest_blob`으로 이 분할을 고정한다. 목록이 변경되면 두 PC 분할을 다시 대조한 후 설정을 갱신해야 한다.

## 상주 일괄 실행

2026-11-07 사용자 지시에 따라 두 PC 모두 다음 예약 작업을 일반 사용자 76kor, Limited, Interactive 권한으로 등록했다.

- `LaoPanelObserverPassive10`: 기존 observer.py run, 관측 간격 10초.
- `LawSupplierV2_ObserverBatch`: supplier_batch.py --service, 공급 간격 10초.
- 로그인 시 자동 시작한다. 상태 파일은 feeder/run/supplier-latest.json, 이력은 supplier-history.jsonl이다.
- 모든 신규 배정을 하나의 원격 Git 커밋으로 먼저 기록한 뒤 독립 패널에 병렬 전송한다. 처리 시간이 10초를 넘으면 중복 배치를 실행하지 않고 다음 회차를 바로 시작한다.
- 중지: 예약 작업 LawSupplierV2_ObserverBatch를 중지하거나 supplier-config.json의 enabled를 false로 변경한다. 기존 STOP 파일도 계속 준수한다.
- 일괄 1회 실행은 supplier_batch.py, 단일 이벤트/워커 진단은 아래 event_supplier.py 명령을 사용한다.

## 단일 이벤트 진단 호출

가동 중인 관측기 `2026.11.07-passive.2`의 `run/latest.json`과 `run/health.json`을 직접 읽는 연결을 포함한다. 관측기 파일이나 프로세스를 변경하지 않는다.

- 현재 6패널을 한 번 처리하는 미리보기: `args=["observer-preview"]`
- 현재 6패널에 대해 한 번 실제 공급/발송확인 처리: `args=["observer-supply"]`
- 한 워커만 처리: `args=["observer-supply", "--worker", "C01"]` (노텍스는 N01 등)
- 두 스냅샷의 boot/sequence가 같고 60초 이내여야 한다. 오류·승인대기·초안·다른 배정·갱신중단 상태에서는 보류한다.
- `COMPLETION_REPORTED`는 READY 요청으로만 변환하며, 실제 완료 판정은 GitHub 검수 결과·영수증을 공급기가 별도로 확인한다.
- 신규 발송 marker가 관측되면 DELIVERY_ACK로 기록한다. 상주 모니터링은 관측기가 담당하며, 공급기는 발송 직전·직후의 대상 패널 상태만 확인한다.
- 기존 `user_stop_all_workers` STOP 파일과 관측 출력의 STOP 제어가 존재하면 공급을 보류한다. 설치는 이를 삭제하거나 해제하지 않는다.

각 PC의 등록 Python 도구로 아래 스크립트를 호출한다. 이벤트는 `stdin`으로 전달하면 된다.

- WORK-C: `C:\YOLLA_WORKSPACE\law_feeder_v2_build_20261107\feeder\event_supplier.py`
- NOTEX: `E:\YOLLA_WORKSPACE\law_feeder_v2_build_20261107\feeder\event_supplier.py`
- 읽기 전용 확인: `args=["inspect"]`
- 배정 미리보기: `args=["preview", "--event", "-"]`
- 실제 공급: `args=["supply", "--event", "-"]`
- `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8` 권장. 표준 출력은 ASCII-safe JSON이다.
- Windows 도구의 stdin 인코딩 차이를 피하도록 이벤트 JSON은 `ensure_ascii=true`로 직렬화한다.

기존 `github_instruction_feeder.py service` 또는 `run-once`와 동시에 실행하지 않는다. 기존 예약 작업은 자동 활성화하지 않는다. supplier_batch.py --service만 상주 실행하고, 기존 공급기를 함께 가동하지 않는다.

## READY 이벤트

아래는 형식 예시이며 실제 관측값으로 채워야 한다. 공급기는 가짜 관측값을 생성하지 않는다.

```json
{
  "schema": "LAW_SUPPLY_EVENT_V1",
  "event_id": "observer-unique-event-id",
  "kind": "READY",
  "pc": "WORK-C",
  "worker": "C01",
  "panel_id": "panel-01",
  "conversation_id": "실제 관측한 대화 ID",
  "observed_at": "실제 관측 시각 ISO8601 + timezone",
  "status": "IDLE",
  "composer_empty": true,
  "expected_assignment_id": "현재 status/active/C01.json의 assignment_id 또는 null"
}
```

`observed_at`은 120초 이내여야 한다. `status`는 IDLE/READY만 가능하다. 실제 패널 대화가 `status/worker-bindings.json`의 verified 바인딩과 같아야 한다. 바인딩이 바뀐 경우 공급기는 HOLD로 멈춘다. 관측기는 공급 직전까지 대화/입력창을 다른 작업에 사용하지 않아야 한다.

공급기는 다음을 직접 검증한다.

1. 기존 ACTIVE가 있으면 같은 가족의 검수와 영수증 모두 COMPLETE인지 확인한다.
2. 한국어 문서목록, 중국어 전체 행 수, 문서별 행 존재, 교정 행 ID/수, 원본 식별자 및 공급 당시 원본 blob을 대조한다.
   기존 교정문 필드 `text`, `new_text`, `after`, `corrected_text`를 읽으며 교정문이 없는 항목은 거절한다. 신규 작업은 `id`와 `text`를 사용한다.
   기존 NOTEX 영수증에서 생략된 job_id는 정확한 파일경로와 review의 job_id·worker·전체 행 수·교정 ID로 교차검증한다. V2 신규 배정 영수증에는 job_id가 필수이며, 잘못 기재된 ID는 허용하지 않는다.
3. 기존 완료·부분결과·ACTIVE·HOLD 가족을 새로 배정하지 않는다. 한 워커는 한 법령가족 전체를 맡는다.
4. 완료 아카이브, 신규 ACTIVE/claim, 발송 의도와 이벤트 영수증을 하나의 Git 커밋으로 먼저 게시한다.
5. 원격 브랜치가 바뀌면 새 스냅샷에서 다시 계산한다. force push나 기존 작업트리 rebase는 하지 않는다.
6. 지시문 입력은 한 번만 시도한다. 입력 후 미전송 초안으로 남으면 같은 배정의 초안과 보내기 버튼을 확인하여 제출을 마친다. 성공 응답은 SENT_AWAITING_OBSERVER_ACK, 불확실 응답/오류는 UNCERTAIN_NO_RESEND로 남긴다.

## 발송 확인

관측기가 워커의 마지막 사용자 메시지에서 반환된 `marker`를 확인하면 별도 새 `event_id`로 DELIVERY_ACK를 호출한다. `expected_assignment_id`는 공급기가 반환한 assignment_id이며 `marker`는 정확히 같은 문자열이어야 한다. READY 이벤트와 동일한 PC/worker/panel/conversation/observed_at/composer_empty 필드가 필요하다. `status`는 실제 관측값(WORKING 포함)을 넣는다.

ACK는 결과를 SENT_VERIFIED로 기록할 뿐 메시지를 보내지 않는다.

## 재시작과 보류

- 같은 event_id와 같은 내용 재호출은 저장된 결과를 반환하고 발송하지 않는다.
- 같은 event_id를 다른 내용으로 재사용하면 HOLD다.
- 발송 전후 프로세스가 종료되어도 원격 발송 의도가 남아 있어 자동 재전송하지 않는다.
- INTENT_RECORDED_NO_RESEND, UNCERTAIN_NO_RESEND는 관측기가 실제 메시지를 확인한 경우에만 ACK할 수 있다. 미확인 상태는 보류한다. 새 event_id로 강제로 다시 보내지 않는다.
- 이전 안전/권한 HOLD, NO_RETRY를 GitHub 경로 변경으로 자동 해제하지 않는다.
- WORKER_NOT_READY, COMPOSER_NOT_EMPTY, STALE_OBSERVER_EVENT, CONVERSATION_CHANGED에는 새 실제 관측이 필요하다.
- 완료 검증 오류는 해당 가족을 유지한다. 공급기가 번역 결과를 수정하거나 완료로 덮어쓰지 않는다.
- GitHub 상태 파일만 게시한다. `laws/*`나 운영 DB를 쓰지 않는다. 구조 검증은 법률 번역 품질을 보증하지 않는다.

## 검증

```text
python -m unittest discover -s feeder -p test_event_supplier.py -v
python -m unittest discover -s feeder -p test_supplier_batch.py -v
python -m unittest discover -s feeder -p test_batch_engine.py -v
```

테스트는 중복 이벤트, 동시 커밋, 발송 timeout, 발송 의도 기록 후 종료, 별표 누락, 부분 검수, 관측 시각/대화/입력창 불일치, ACK 및 원격 Git 트랜잭션을 확인한다. 실제 워커에게 테스트 메시지를 전송하지 않는다.
