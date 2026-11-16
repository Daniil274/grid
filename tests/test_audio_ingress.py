import io
import wave

import pytest

from web_chat.audio_ingress import route_audio, validate_audio_payload


def test_audio_route_requires_explicit_runtime_capability():
    assert route_audio(explicitly_selected=True, runtime_accepts_audio=True).route == "native"
    assert route_audio(explicitly_selected=True, runtime_accepts_audio=False).route == "transcribe"
    assert route_audio(explicitly_selected=False, runtime_accepts_audio=True).route == "transcribe"


@pytest.mark.parametrize("data,media_type,max_bytes", [
    (b"", "audio/wav", 10),
    (b"01234567890", "audio/wav", 10),
    (b"x", "text/plain", 10),
])
def test_payload_validation_rejects_invalid_input(data, media_type, max_bytes):
    with pytest.raises(ValueError):
        validate_audio_payload(data, media_type, max_bytes=max_bytes)


def test_payload_validation_accepts_audio_with_parameters():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x00\x00")
    data = output.getvalue()
    validate_audio_payload(data, "audio/wav; codecs=1", max_bytes=len(data))
