"""Voice notes: Ogg/Opus through ffmpeg, and a clear skip when ffmpeg is absent."""

import io
import struct
import wave

import pytest

from web_chat.telegram import audio
from web_chat.telegram.config import Settings
from web_chat.telegram.runner import TurnOutcome
from web_chat.telegram.service import TelegramChannel
from web_chat.telegram.store import TelegramStore

NOW = 1_000_000.0


def wav_bytes(seconds: float = 0.4, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 0) * int(rate * seconds))
    return buffer.getvalue()


class Bot:
    def __init__(self, clip=b"OggS-fake"):
        self.sent = []
        self.voices = []
        self.clip = clip

    async def send_message(self, chat_id, text, thread_id=None, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text})
        return {"message_id": len(self.sent)}

    async def send_voice(self, chat_id, ogg, thread_id=None, duration=0):
        self.voices.append({"chat_id": chat_id, "ogg": ogg})
        return {}

    async def download(self, file_id):
        return self.clip


class Voice:
    """Stands in for web_chat.voice.VoiceService."""

    def __init__(self, text="hello from a voice note", wav=None):
        self.transcribed = []
        self.text = text
        self.wav = wav or wav_bytes()

    async def transcribe(self, wav_data):
        self.transcribed.append(wav_data)
        return {"text": self.text}

    async def synthesize(self, text):
        return self.wav


class Spaces:
    def __init__(self, outcome=None):
        self.outcome = outcome or TurnOutcome("answered", answer="done")
        self.asked = []

    def use(self, _user_id):
        class Lease:
            async def __aenter__(self):
                return SimpleSpace()

            async def __aexit__(self, *exc):
                return False

        return Lease()


class SimpleSpace:
    def awaiting_approval(self):
        return {}

    def answer_action_review(self, *a, **k):
        return False


def make_channel(tmp_path, bot, voice, *, voice_replies=False, runner=None):
    store = TelegramStore(tmp_path / "telegram.db")
    store.redeem_code(store.create_code("alice", now=NOW)["code"], 11, 0, now=NOW)
    service = TelegramChannel(
        store, Spaces(), Settings(enabled=True, token="1:x", voice_replies=voice_replies),
        bot=bot, runner=runner, voice_factory=lambda: voice, clock=lambda: NOW,
    )
    return service


def voice_update(file_id="voice-1"):
    return {"update_id": 1, "message": {
        "chat": {"id": 11, "type": "private"}, "from": {"id": 11, "username": "alice"},
        "voice": {"file_id": file_id, "duration": 2},
    }}


async def test_without_ffmpeg_a_voice_note_is_refused_with_advice(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "ffmpeg_path", lambda: None)
    assert audio.available() is False
    with pytest.raises(audio.AudioUnavailable):
        audio.to_wav_sync(b"OggS")

    bot, voice = Bot(), Voice()
    service = make_channel(tmp_path, bot, voice)
    await service.dispatch(voice_update())
    assert "ffmpeg" in bot.sent[-1]["text"]
    assert voice.transcribed == []


async def test_a_voice_note_is_converted_to_wav_before_recognition(tmp_path, monkeypatch):
    service_voice = Voice()
    converted = []

    async def fake_to_wav(data):
        converted.append(data)
        return wav_bytes()

    monkeypatch.setattr(audio, "available", lambda: True)
    monkeypatch.setattr(audio, "to_wav", fake_to_wav)
    bot = Bot()

    class Runner:
        def __init__(self):
            self.messages = []

        async def run(self, user_id, context_id, message, on_watch=None):
            self.messages.append(message)
            return TurnOutcome("answered", answer="done")

    runner = Runner()
    service = make_channel(tmp_path, bot, service_voice, runner=runner)
    await service.dispatch(voice_update())
    assert converted == [b"OggS-fake"]
    assert service_voice.transcribed and service_voice.transcribed[0].startswith(b"RIFF")
    assert runner.messages == ["hello from a voice note"]
    assert bot.sent[-1]["text"] == "done"


async def test_a_voice_reply_falls_back_to_text_without_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(audio, "ffmpeg_path", lambda: None)
    bot = Bot()

    class Runner:
        async def run(self, user_id, context_id, message, on_watch=None):
            return TurnOutcome("answered", answer="the answer")

    service = make_channel(tmp_path, bot, Voice(), voice_replies=True, runner=Runner())
    await service.dispatch({"update_id": 1, "message": {
        "chat": {"id": 11, "type": "private"}, "from": {"id": 11, "username": "alice"},
        "text": "hi"}})
    assert bot.voices == []
    assert bot.sent[-1]["text"] == "the answer"


async def test_a_voice_reply_is_sent_as_ogg(tmp_path, monkeypatch):
    class FakeBot(Bot):
        async def send_voice(self, chat_id, ogg, thread_id=None, duration=0):
            self.voices.append({"chat_id": chat_id, "ogg": ogg})
            return {}

    async def fake_to_ogg(data):
        return b"OggS-voice"

    monkeypatch.setattr(audio, "ffmpeg_path", lambda: "/usr/bin/ffmpeg")
    monkeypatch.setattr(audio, "to_ogg", fake_to_ogg)

    class Runner:
        async def run(self, user_id, context_id, message, on_watch=None):
            return TurnOutcome("answered", answer="the answer")

    bot = FakeBot()
    service = make_channel(tmp_path, bot, Voice(), voice_replies=True, runner=Runner())
    await service.dispatch({"update_id": 1, "message": {
        "chat": {"id": 11, "type": "private"}, "from": {"id": 11, "username": "alice"},
        "text": "hi"}})
    assert bot.voices and bot.voices[0]["ogg"] == b"OggS-voice"


@pytest.mark.skipif(not audio.available(), reason="ffmpeg is not installed")
def test_ffmpeg_converts_both_ways_and_keeps_speech_parameters():
    wav = wav_bytes()
    ogg = audio.to_ogg_sync(wav)
    assert ogg[:4] == b"OggS"
    back = audio.to_wav_sync(ogg)
    assert back[:4] == b"RIFF"
    with wave.open(io.BytesIO(back), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == audio.WAV_RATE
        assert handle.getsampwidth() == 2
