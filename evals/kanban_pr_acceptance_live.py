"""Reported-CI local HTTP/lifecycle probe; no live GitHub access or inference.
Run: python evals/kanban_pr_acceptance_live.py ROOT
"""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
import pytest
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect

def create(conn, title):
    return kb.create_task(conn, title=title, completion_contract='acme/repo')

fixture_root = Path(sys.argv[2]) if len(sys.argv) > 2 else root
spec = importlib.util.spec_from_file_location('http_fixture', fixture_root / 'tests/hermes_cli/test_kanban_pr_acceptance.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
patch = pytest.MonkeyPatch()
with tempfile.TemporaryDirectory(prefix='hermes-pr-live-') as home:
    setup = fixture.github.__wrapped__(Path(home), patch)
    state = next(setup)
    reports = []
    try:
        with connect() as conn:
            for outcome in ('failure', 'cancelled', 'timed_out', 'neutral', 'skipped', 'success'):
                state.update(conclusion=outcome)
                tid = create(conn, 'PR acceptance live')
                accepted = kb.complete_task(conn, tid, metadata={'published_pr': 'https://github.com/acme/repo/pull/7'})
                row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance' ORDER BY id DESC", (tid,)).fetchone()
                receipt = json.loads(row[0]) if row else None
                assert accepted is (outcome == 'success')
                assert receipt and receipt['acceptance_basis'] == 'reported-ci'
                reports.append({'case': outcome, 'accepted': accepted, 'status': kb.get_task(conn, tid).status, 'receipt': receipt})
            for outcome in ('success', 'failure'):
                tid = kb.create_task(conn, title='Concurrent ownership', completion_contract='acme/repo')
                owner = kb.claim_task(conn, tid)
                run_id = owner.current_run_id
                def reclaim():
                    with connect() as other:
                        kb.block_task(other, tid, reason='Reassigned during acceptance')
                        kb.unblock_task(other, tid)
                        state['replacement'] = kb.claim_task(other, tid).current_run_id
                state.update(conclusion=outcome, race=reclaim)
                accepted = kb.complete_task(conn, tid, expected_run_id=run_id, metadata={'published_pr': 'https://github.com/acme/repo/pull/7'})
                assert not accepted
                assert kb.get_task(conn, tid).current_run_id == state['replacement']
                reports.append({'case': 'CAS-'+outcome, 'accepted': accepted, 'run_id': kb.get_task(conn, tid).current_run_id,
                    'receipts': conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()[0]})
                del state['race']
        print(json.dumps({'reports': reports, 'requests': state['requests']}, indent=2))
    finally:
        try:
            next(setup)
        except StopIteration:
            pass
        patch.undo()
