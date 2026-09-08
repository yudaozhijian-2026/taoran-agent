"""Durable, low-priority observations. This queue never writes feedback or scores."""

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Thread
from time import time

logger = logging.getLogger(__name__)
POLICY = 'async-observation-v1'


@contextmanager
def connect(settings):
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.database_path, timeout=3) as db:
        db.row_factory = sqlite3.Row
        db.execute('''CREATE TABLE IF NOT EXISTS semantic_observation_jobs (
            observation_id TEXT PRIMARY KEY, source_hash TEXT NOT NULL,
            candidate_hash TEXT NOT NULL, payload TEXT NOT NULL,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, available_at REAL NOT NULL,
            started_at REAL, completed_at REAL, result TEXT)''')
        yield db


def enqueue(settings, payload):
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    candidate_hash = hashlib.sha256(json.dumps(payload['candidate'], ensure_ascii=False,
        sort_keys=True).encode()).hexdigest()
    source_hash = hashlib.sha256(json.dumps(payload['context'], ensure_ascii=False,
        sort_keys=True).encode()).hexdigest()
    oid = 'obs_' + hashlib.sha256((POLICY + encoded).encode()).hexdigest()
    now = time()
    with connect(settings) as db:
        db.execute('INSERT OR IGNORE INTO semantic_observation_jobs '
                   '(observation_id,source_hash,candidate_hash,payload,status,created_at,available_at) '
                   'VALUES (?,?,?,?,?,?,?)', (oid, source_hash, candidate_hash, encoded, 'queued', now, now))
    return oid


def snapshot(settings):
    with connect(settings) as db:
        counts = dict(db.execute('SELECT status,count(*) FROM semantic_observation_jobs GROUP BY status'))
        oldest = db.execute("SELECT min(created_at) FROM semantic_observation_jobs WHERE status='queued'").fetchone()[0]
    return {**{s: counts.get(s, 0) for s in ('queued', 'running', 'completed', 'failed')},
            'oldest_queued_seconds': max(0, int(time()-oldest)) if oldest else 0,
            'concurrency': 1, 'policy': POLICY}


def recover(settings):
    with connect(settings) as db:
        db.execute("UPDATE semantic_observation_jobs SET status=CASE WHEN attempts<2 THEN 'queued' "
                   "ELSE 'failed' END,available_at=?,result=? WHERE status='running'",
                   (time(), json.dumps({'failure': 'worker_interrupted'})))


def run_one(settings, reviewer):
    # Do not queue behind interactive or formal calls; retry admission next tick.
    cap = reviewer.model_capacity.snapshot()
    if cap['active_frontend'] or cap['active_backend']:
        return False
    with connect(settings) as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT * FROM semantic_observation_jobs WHERE status='queued' "
                         'AND available_at<=? ORDER BY created_at LIMIT 1', (time(),)).fetchone()
        if row is None:
            return False
        db.execute("UPDATE semantic_observation_jobs SET status='running',attempts=attempts+1,"
                   'started_at=? WHERE observation_id=?', (time(), row['observation_id']))
    payload = json.loads(row['payload'])
    candidate = payload['candidate']
    audit, details = {}, {}
    failed = False
    try:
        reviewer._experimental_audit_wording(payload['context'], payload['analysis'],
            [p.get('suggestion', '') for p in candidate.get('items', [])],
            min(settings.frontend_model_timeout_seconds, 20), audit,
            analysis_points=[(p['kind'], p['text'], []) for p in candidate['analysis_points']],
            suggestion_codes=[p['code'] for p in candidate.get('items', [])], repair_details=details)
    except Exception as exc:  # noqa: BLE001 - observation failure never touches delivered work
        failed = not bool(details.get('semantic_issues'))
        code = str(exc)
        audit['failure'] = code if code.startswith('wording_experimental_audit_') else type(exc).__name__
    result = json.dumps({'policy': 'observe_only', 'audit': audit, 'details': details,
                         'identity': payload.get('identity', {}),
                         'source_hash': row['source_hash'], 'candidate_hash': row['candidate_hash']},
                        ensure_ascii=False)
    status = 'queued' if failed and row['attempts'] < 1 else 'failed' if failed else 'completed'
    with connect(settings) as db:
        db.execute('UPDATE semantic_observation_jobs SET status=?,result=?,available_at=?,completed_at=? '
                   'WHERE observation_id=?', (status, result, time()+5,
                    None if status == 'queued' else time(), row['observation_id']))
    return True


def start(settings, original_reviewer):
    from .front_v46 import bind
    reviewer = bind(original_reviewer)
    reviewer.observation_workload = 'backend'
    reviewer.observation_max_attempts = 2
    stop = Event()
    recover(settings)

    def work():
        while not stop.is_set():
            try:
                worked = run_one(settings, reviewer)
            except Exception:
                logger.exception('semantic observation worker error')
                worked = False
            stop.wait(0.25 if worked else 1)

    thread = Thread(target=work, name='taoran-observation', daemon=True)
    thread.start()
    return stop, thread
