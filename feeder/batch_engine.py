"""Plan all ready slots in one Git transaction, then transmit to independent panels together."""
import copy
import socket
from concurrent.futures import ThreadPoolExecutor, as_completed
import event_supplier as s
import recovery_engine as recovery


class Overlay:
    def __init__(self, base):
        self.base, self.sha = base, base.sha
        self.paths, self.changes = set(base.paths), {}

    def read(self, path, default=None):
        return copy.deepcopy(self.changes[path]) if path in self.changes else self.base.read(path, default)

    def text(self, path):
        return s.encoded(self.changes[path]) if path in self.changes else self.base.text(path)

    def blob(self, path):
        return self.base.blob(path)

    def apply(self, changes):
        self.changes.update(changes)
        for path, value in changes.items():
            if value is None:
                self.paths.discard(path)
            else:
                self.paths.add(path)


def prepare(config, snapshot, report, health):
    view, results, pending = Overlay(snapshot), {}, {}
    for worker in config['workers']:
        if not worker.get('enabled'):
            continue
        ident = worker['worker']
        try:
            active = s.active_records(view).get(ident)
            panel = next((p for p in report.get('panels', []) if p.get('panel_id') == worker['panel_id']), {})
            if config.get('handoff_run'):
                import worker_handoff as handoff
                if handoff.journal(config, ident).exists() or (active and handoff.f.reason(panel)):
                    results[ident] = {'worker': ident, 'status': 'HANDOFF_PENDING', 'sent': False}
                    continue
            view.apply(recovery.acknowledge(view, active, panel))
            event = s.observer_event(config, report, health, ident, active, allow_recovery=True)
            work = s.plan(view, config, event)
            view.apply(work['changes'])
            results[ident] = {'worker': ident, **work['result'], 'sent': False}
            if event['kind'] == 'DELIVERY_ACK':
                panel = next(p for p in report['panels'] if p['panel_id'] == event['panel_id'])
                if panel['native_state'] in ('IDLE', 'READY'):
                    event = s.observer_event(config, report, health, ident, s.active_records(view).get(ident), allow_recovery=True)
                    work = s.plan(view, config, event)
                    view.apply(work['changes'])
                    results[ident] = {'worker': ident, **work['result'], 'sent': False}
            if work['result']['status'] == 'WAIT_ACTIVE_FAMILY':
                work = recovery.prepare(view, config, event, panel, s.active_records(view)[ident])
                view.apply(work['changes'])
                results[ident] = {'worker': ident, **work['result'], 'sent': False}
            if work.get('prompt'):
                pending[ident] = {'event': event, 'work': work}
        except Exception as exc:
            error = str(exc) if isinstance(exc, s.SupplyError) else type(exc).__name__
            results[ident] = {'worker': ident, 'status': 'BUSY' if error == 'OBSERVER_WORKER_NOT_READY' else 'HOLD',
                              'sent': False, 'error': error}
    return view.changes, results, pending


def execute(config, store, sender=s.send_panel):
    s.check(socket.gethostname().upper() == config['pc'].upper(), 'WRONG_EXECUTION_HOST')
    s.check(config.get('enabled') is True, 'SUPPLIER_DISABLED')
    s.require_unstopped(config)
    with store.lock():
        for attempt in range(3):
            snapshot = store.refresh()
            report, health = s.read_observation(config)
            changes, results, pending = prepare(config, snapshot, report, health)
            if not changes:
                return {'pc': config['pc'], 'results': list(results.values()), 'preview': False, 'batch': True}
            try:
                commit = store.publish(snapshot.sha, changes, 'Supply observer batch ' + str(report['sequence']))
                break
            except s.Conflict:
                if attempt == 2:
                    raise
        if not pending:
            return {'pc': config['pc'], 'results': list(results.values()), 'commit': commit, 'preview': False, 'batch': True}
        latest = store.refresh()
        jobs = s.manifest(latest, config)
        ready = {}
        for ident, item in pending.items():
            result = results[ident]
            try:
                s.validate_event(item['event'], config)
                s.require_unstopped(config)
                current = latest.read('status/active/' + ident + '.json')
                s.check(current and current['assignment_id'] == result['assignment_id'], 'CLAIM_CHANGED_BEFORE_SEND')
                s.binding_matches(latest, item['event'], config)
                s.check(s.source(latest, jobs[current['job_id']])[2] == current['source_blobs'], 'SOURCE_CHANGED_BEFORE_SEND')
                ready[ident] = item
            except Exception as exc:
                result.update(status='UNCERTAIN_NO_RESEND', sent=False,
                              error=str(exc) if isinstance(exc, s.SupplyError) else type(exc).__name__)
        # Independent panel deliveries share one durable batch intent. No message retry.
        with ThreadPoolExecutor(max_workers=max(1, len(ready))) as pool:
            futures = {pool.submit(sender, config, item['event'], item['work']['prompt']): ident
                       for ident, item in ready.items()}
            for future in as_completed(futures):
                ident = futures[future]
                try:
                    future.result()
                    results[ident].update(status='SENT_AWAITING_OBSERVER_ACK', sent=True)
                except Exception as exc:
                    results[ident].update(status='UNCERTAIN_NO_RESEND', sent=None,
                                         error=str(exc) if isinstance(exc, s.SupplyError) else type(exc).__name__)
        for ident in pending:
            results[ident].update(attempted_at=s.iso(), claim_commit=commit)
        for attempt in range(3):
            try:
                fresh = store.refresh()
                updates = {}
                for ident in pending:
                    result = results[ident]
                    current = fresh.read('status/active/' + ident + '.json')
                    if not current or current['assignment_id'] != result['assignment_id']:
                        result['publication'] = 'CLAIM_CHANGED_RECORD_PENDING'
                        continue
                    if result.get('recovery'):
                        path = result['record_path']
                        record = fresh.read(path)
                        record.update(status='RECOVERY_SENT_AWAITING_ACK' if result.get('sent') is True else 'RECOVERY_UNCERTAIN_NO_RESEND',
                                      attempted_at=result['attempted_at'], delivery_result=result)
                        updates[path] = record
                        continue
                    current['delivery_status'] = result['status']
                    updates.update({'status/active/' + ident + '.json': current,
                                    'status/claims/' + current['assignment_id'] + '.json': current,
                                    'status/delivery/' + current['assignment_id'] + '.json': result,
                                    'status/supply-events/' + result['event_id'] + '.json': result})
                if updates:
                    delivery_commit = store.publish(fresh.sha, updates, 'Record observer batch deliveries')
                    for ident in pending:
                        results[ident]['delivery_commit'] = delivery_commit
                break
            except s.Conflict:
                continue
            except Exception:
                for ident in pending:
                    results[ident]['publication'] = 'DELIVERY_RECORD_PENDING_NO_RESEND'
                break
        return {'pc': config['pc'], 'results': list(results.values()), 'commit': commit,
                'preview': False, 'batch': True, 'observer_sequence': report['sequence']}


