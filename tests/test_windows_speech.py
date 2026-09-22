import json
from pathlib import Path
from types import SimpleNamespace

from core import windows_speech


def test_windows_tts_passes_text_as_data_and_hides_window(tmp_path, monkeypatch):
    text = 'Привет; $(Remove-Item something) " <speak> & `literal`'
    monkeypatch.setattr(windows_speech, "available", lambda: True)
    monkeypatch.setattr(windows_speech.shutil, "which", lambda _: "powershell.exe")
    seen = {}

    def run(command, **kwargs):
        seen.update(command=command, **kwargs)
        payload = json.loads(kwargs["input"])
        assert payload["text"] == text
        Path(payload["path"]).write_bytes(b"RIFFtest")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(windows_speech.subprocess, "run", run)
    path = windows_speech.synthesize(text, str(tmp_path))
    assert Path(path).read_bytes() == b"RIFFtest"
    assert text not in " ".join(seen["command"])
    assert "shell" not in seen
    assert seen["timeout"] == 90
