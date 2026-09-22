"""Offline Windows speech fallback when a Silero model isn't installed.

Text and paths are passed as JSON on stdin, never interpolated into shell code.
The caller owns the output directory and runs this blocking function in a worker.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import uuid


_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
Add-Type -AssemblyName System.Speech
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {
    $culture = [System.Globalization.CultureInfo]::GetCultureInfo($request.language)
    $voice = $synth.GetInstalledVoices() | Where-Object {
        $_.Enabled -and $_.VoiceInfo.Culture.Name -eq $culture.Name
    } | Select-Object -First 1
    if (-not $voice) { throw "No installed voice for $($culture.Name)" }
    $synth.SelectVoice($voice.VoiceInfo.Name)
    $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(
        [int]$request.sample_rate,
        [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
        [System.Speech.AudioFormat.AudioChannel]::Mono)
    $synth.SetOutputToWaveFile($request.path, $format)
    $synth.Speak([string]$request.text)
} finally {
    $synth.Dispose()
}
"""


def available() -> bool:
    return os.name == "nt" and shutil.which("powershell.exe") is not None


def synthesize(text: str, out_dir: str, *, language: str = "ru-RU", sample_rate: int = 24000) -> str:
    if not available():
        raise RuntimeError("Windows speech requires Windows PowerShell and an installed voice.")
    output = Path(out_dir) / f"tts_{uuid.uuid4().hex}.wav"
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive",
        "-EncodedCommand", base64.b64encode(_SCRIPT.encode("utf-16-le")).decode("ascii"),
    ]
    payload = json.dumps({
        "text": text, "path": str(output.resolve()), "language": language,
        "sample_rate": sample_rate,
    }, ensure_ascii=False).encode("utf-8")
    try:
        result = subprocess.run(
            command, input=payload, capture_output=True, timeout=90,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Windows speech synthesis timed out.") from exc
    if result.returncode or not output.is_file():
        # PowerShell stderr can contain the submitted text; keep it out of logs.
        raise RuntimeError(f"Windows speech failed. Install a voice for {language} in Windows settings.")
    return str(output)
