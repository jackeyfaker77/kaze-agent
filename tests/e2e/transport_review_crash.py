"""Hard exit across the operator decision and queue transition transaction."""
import os
from pathlib import Path
import sys
from bus.durable import TransportStore

store = TransportStore(Path(sys.argv[1]))
store.put('job', 'test', {'channel': 'test', 'chat_id': '1'})
with store.db:
    store.db.execute("INSERT INTO parts VALUES ('job','1','{}','done','{\"message_id\":\"one\"}')")
    store.db.execute("INSERT INTO parts VALUES ('job','2','{}','sending',NULL)")
store.state('job', 'needs_review')
if sys.argv[3] == 'before_commit':
    store.db.create_function('hard_exit', 0, lambda: os._exit(73))
    store.db.execute('''CREATE TEMP TRIGGER exit_review AFTER INSERT ON transport_reviews
        BEGIN SELECT hard_exit(); END''')
store.review_job('job', expected_revision=store.get('job')['revision'], action_id='review',
                 decision=sys.argv[2], note='Verified target', absent_parts=['2'] if sys.argv[2] == 'retry' else [],
                 accounts={}, handlers={'test': object()})
os._exit(73)
