import logging

from chronicle.builder import main as builder_main


def test_placeholder_runs_and_logs_without_error(caplog, monkeypatch) -> None:
    monkeypatch.setenv("CHRONICLE_DATA_DIR", "/tmp")
    with caplog.at_level(logging.INFO, logger="chronicle.builder"):
        builder_main.run()
    messages = [record.message for record in caplog.records]
    assert any("placeholder" in message for message in messages)
