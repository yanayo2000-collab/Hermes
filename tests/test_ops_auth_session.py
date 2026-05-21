import sqlite3

from app.main import Database, OpsAuthManager


def test_session_user_returns_user_when_last_seen_touch_is_locked(monkeypatch):
    db = Database(':memory:')
    auth = OpsAuthManager(db)
    user = auth.create_user(
        username='admin01',
        password='secret123',
        role='super_admin',
        display_name='Admin',
    )
    token = auth.create_session(user)

    def raise_locked(*args, **kwargs):
        raise sqlite3.OperationalError('database is locked')

    monkeypatch.setattr(auth, '_touch_session_last_seen', raise_locked)

    session_user = auth.session_user(token)

    assert session_user is not None
    assert session_user['user_id'] == user['user_id']
