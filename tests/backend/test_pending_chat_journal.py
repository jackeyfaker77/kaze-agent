import json
import pytest
from desktop_bridge.pending_chat_journal import PendingChatJournal


def test_failed_atomic_replace_keeps_previous_recovery_record(tmp_path, monkeypatch):
    journal = PendingChatJournal(tmp_path)
    original = [{'role': 'user', 'content': 'original', 'desktop_write_id': 'original'}]
    journal.save('desktop:key', original, 'title', None)
    def fail(*args):
        raise OSError('replace failed')
    monkeypatch.setattr('desktop_bridge.pending_chat_journal.os.replace', fail)
    with pytest.raises(OSError, match='replace failed'):
        journal.save('desktop:key', original + [{'role': 'user', 'content': 'next', 'desktop_write_id': 'next'}], 'title', None)
    assert PendingChatJournal(tmp_path).load()['desktop:key']['messages'] == original
    assert not list(journal.directory.glob('*.tmp'))


def test_fsync_failure_never_publishes_incomplete_journal(tmp_path, monkeypatch):
    journal = PendingChatJournal(tmp_path)
    def fail(*args):
        raise OSError('flush failed')
    monkeypatch.setattr('desktop_bridge.pending_chat_journal.os.fsync', fail)
    with pytest.raises(OSError, match='flush failed'):
        journal.save('desktop:key', [{'desktop_write_id': 'one'}], 'title', None)
    assert journal.load() == {}


def test_corrupt_journal_is_not_silently_dropped(tmp_path):
    journal = PendingChatJournal(tmp_path)
    journal.save('desktop:key', [{'desktop_write_id': 'one'}], 'title', None)
    path = next(journal.directory.glob('*.json'))
    path.write_text('{broken', encoding='utf-8')
    with pytest.raises(json.JSONDecodeError):
        journal.load()
    assert path.read_text(encoding='utf-8') == '{broken'
