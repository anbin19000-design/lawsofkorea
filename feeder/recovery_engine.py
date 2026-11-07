"""Bounded continuation inside the existing supplier; no scheduler or allocator."""
import re
import datetime as dt
import event_supplier as s


def path_for(active):
    return 'status/delivery/recovery-' + active['assignment_id'] + '.json'


def acknowledge(snapshot, active, row):
    if not active:
        return {}
    path = path_for(active)
    record = snapshot.read(path)
    if not record or row.get('recovery_marker') != record.get('recovery_marker'):
        return {}
    if (row.get('observation_ok') is not True or row.get('url') != record.get('url')
            or not -5 <= s.now().timestamp() - row.get('observed_at', 0) <= 60
            or (row.get('assignment') or {}).get('job_id') != active['job_id']):
        return {}
    changed = False
    if not record.get('received_at'):
        record.update(received_at=s.iso(), status='RECOVERY_RECEIVED'); changed = True
    if row.get('state') == 'WORKING' and not record.get('working_seen_at'):
        record.update(working_seen_at=s.iso(), status='RECOVERY_WORKING_VERIFIED'); changed = True
    return {path: record} if changed else {}


def prepare(snapshot, config, event, row, active):
    """One durable instruction per observed episode; never replay failed delivery."""
    path = path_for(active)
    old = snapshot.read(path)
    base = dict(worker=active['worker'], job_id=active['job_id'], assignment_id=active['assignment_id'])
    episode = 1
    if old:
        # A later benign pause is a new episode only after verified receipt AND
        # working, a new assistant response, and a quiet interval. Storage errors,
        # uncertain sends and protected reports are never retried here.
        try:
            age = s.now().timestamp() - dt.datetime.fromisoformat(old['created_at']).timestamp()
        except (KeyError, ValueError, TypeError):
            age = 0
        count = old.get('assistant_count')
        excerpt = row.get('assistant_excerpt', '')
        benign = (row.get('state') == 'IDLE' and row.get('native_state') in ('IDLE', 'READY')
                  and row.get('recovery_marker') == old.get('recovery_marker')
                  and old.get('received_at') and old.get('working_seen_at')
                  and isinstance(count, int) and isinstance(row.get('assistant_count'), int)
                  and row['assistant_count'] > count and age >= 90
                  and not re.search(r'error|fail|denied|hold|no_retry|오류|실패|거절|거부|권한|보류|중단|차단', excerpt, re.I))
        episode = old.get('episode', 1) + 1
        if not benign or episode > 3:
            status = old['status']
            if old.get('working_seen_at') and row.get('native_state') in ('IDLE', 'READY'):
                status = 'RECOVERY_PAUSED_AGAIN_REQUIRES_REVIEW'
            return {'result': {**base, 'status': status, 'recovery': True}, 'changes': {}}
    if row.get('recovery_marker') and not old:
        return {'result': {**base, 'status': 'MANUAL_RECOVERY_PRESERVED'}, 'changes': {}}
    if row.get('state_seconds', 0) < 20:
        return {'result': {**base, 'status': 'WAIT_STABLE_INCOMPLETE'}, 'changes': {}}
    s.check(active.get('schema') == 'LAW_SUPPLIER_ASSIGNMENT_V2'
            and active.get('delivery_status') == 'SENT_VERIFIED', 'RECOVERY_REQUIRES_VERIFIED_ASSIGNMENT')
    s.check(row.get('state') in ('IDLE', 'READY', 'COMPLETION_REPORTED', 'STORAGE_BLOCKED', 'REPORTED_ERROR'),
            'PROTECTED_OR_UNOBSERVABLE_WORKER')
    s.check(not any(p.startswith('holds/') and (active['job_id'] in p or active['worker'] in p)
                    for p in snapshot.paths), 'RECOVERY_PROTECTED_REGISTRY')
    job = s.manifest(snapshot, config)[active['job_id']]
    s.check(not re.search(r'HOLD|BLOCK|DENIED|NO_RETRY', str(job.get('prior_queue_status', '')), re.I),
            'RECOVERY_PROTECTED_QUEUE')
    s.check(s.source(snapshot, job)[2] == active['source_blobs'], 'SOURCE_CHANGED_BEFORE_RECOVERY')
    rid = 'R1-' + s.digest(config['pc'] + ':' + active['assignment_id'] + ('' if episode == 1 else ':' + str(episode)))[:24]
    marker = '[LAW-RECOVERY-V1:' + rid + ']'
    text = active['marker'] + ' ' + marker + f'''
저장소: {config['repository']}
브랜치: {config['branch']}
법령가족: {active['title']}
Job ID: {active['job_id']}
같은 배정의 미완료 유휴가 반복 관측되어 이번 중간 정지에 대한 원인 확인 및 재개를 1회 지시합니다. 재개 회차: {episode}.
먼저 기존 대화·오류 응답·공식 GitHub 저장 상태를 읽어 멈춘 원인을 확인하십시오. 401/403, 권한·안전 거절, 승인·로그인 대기, 사용자의 STOP, SAFETY_HOLD/SOURCE_HOLD/RETIRED_HOLD/NO_RETRY 또는 원본 변경이면 재시도·우회하지 말고 정확한 원인과 필요한 사용자 조치만 보고하십시오.
단순 중간 응답 종료나 미완료 작업이면 기존 저장 지점부터 같은 법령가족의 남은 범위만 이어가십시오. 이미 검수·저장한 행은 반복하지 말고, 로컬 DB·worker_client로 전환하지 마십시오.
저장 오류이면 기존 도구 응답의 코드와 원문을 먼저 확인하십시오. 권한 거절이 없는 GithubGraphQLAPIError/UNKNOWN 실행 오류로 확인되는 경우에만 보존된 결과 커밋을 읽고 원본 ko/zh/en 해시 불변·결과 파일 부재를 확인한 뒤 공식 GitHub create_file(Contents API)로 review/zh/{active['job_id']}.json, receipts/{active['job_id']}.json 순서로 저장하십시오. 이 복구는 별도 커밋 2개를 허용합니다. 같은 update_ref를 반복하거나 강제 갱신하지 마십시오. 다른 오류·원인 불명은 오류 원문을 보고하고 중단하십시오.
이미 저장된 파일은 덮어쓰지 말고 검증하십시오. 한 파일만 저장되어 있다면 그 내용·전체 검수 범위를 검증한 뒤 누락된 대응 파일만 저장하십시오. 완료 여부는 두 파일을 브랜치에서 다시 읽어 전체 문서·행 수·고유 교정행 수·COMPLETE와 SHA를 검증한 뒤 보고하십시오.
응답에는 현재 Job ID와 확인한 원인, 재개한 범위, 저장 결과를 명시하십시오. 다른 법령은 배정하지 말고 다음 일은 기존 공급기를 기다리십시오. 운영 DB는 변경하지 않습니다.
'''
    record = {**base, 'schema': 'LAW_RECOVERY_ONCE_V1', 'recovery_marker': marker,
              'episode': episode, 'assistant_count': row.get('assistant_count'),
              'previous_episodes': ((old or {}).get('previous_episodes', []) +
                  [{k:v for k,v in old.items() if k != 'previous_episodes'}]) if old else [],
              'event_id': event['event_id'], 'status': 'RECOVERY_INTENT_NO_RESEND', 'created_at': s.iso(),
              'prompt_sha256': s.digest(text), 'assignment_snapshot': active,
              'url': row['url'], 'observed_state': row['state'], 'observed_at': row['observed_at'],
              'assistant_excerpt': row.get('assistant_excerpt', ''), 'source_head': snapshot.sha}
    return {'result': {**base, 'event_id': event['event_id'], 'status': record['status'],
                       'recovery': True, 'record_path': path},
            'changes': {path: record}, 'prompt': text}

