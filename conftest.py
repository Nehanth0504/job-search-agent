import pytest


@pytest.fixture(autouse=True)
def _keywords_off_by_default(monkeypatch):
    """Tests decide auto keyword adding explicitly, whatever config.yaml says today."""
    import tailor
    monkeypatch.setattr(tailor, "_auto_keywords_setting", lambda: False)


class _AllBoards:
    def __contains__(self, name):
        return True


@pytest.fixture(autouse=True)
def _boards_already_read(monkeypatch):
    """Tests' fake boards count as read before (undated test postings are new), and nothing is written to data/."""
    import pipeline
    monkeypatch.setattr(pipeline, "load_boards_read", lambda: _AllBoards())
    monkeypatch.setattr(pipeline, "save_boards_read", lambda names: None)


@pytest.fixture(autouse=True)
def _no_claude(monkeypatch):
    """Tests never call the Claude API."""
    import tailor
    monkeypatch.setattr(tailor, "_claude_ready", lambda: False)
