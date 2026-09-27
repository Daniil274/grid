"""Generate a reproducible synthetic Russian speech evaluation corpus."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import time
import wave
from pathlib import Path

import httpx
import numpy as np
from dotenv import load_dotenv


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
API_URL = "https://openrouter.ai/api/v1/audio/speech"
FISH = {"id": "fish-free", "model": "fish-audio/s2.1-pro-free:free", "voice": None, "price_per_char": 0.0}
GROK_VOICES = ("eve", "ara", "rex", "sal", "leo")
TEMPORARY_STATUSES = {429, 502, 503, 524, 529}


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def safe_run(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {result.stderr[-1200:]}")


def synthesize(client: httpx.Client, engine: dict, text: str, target: Path, force: bool) -> str | None:
    if target.exists() and target.stat().st_size > 1024 and not force:
        return None
    body = {"model": engine["model"], "input": text, "response_format": "mp3"}
    if engine.get("voice"):
        body["voice"] = engine["voice"]
    for attempt in range(6):
        response = client.post(API_URL, json=body)
        if response.status_code in TEMPORARY_STATUSES and attempt < 5:
            retry_after = response.headers.get("retry-after")
            delay = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else min(12, 1.5**attempt)
            time.sleep(delay)
            continue
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type != "audio/mpeg" or len(response.content) < 1024:
            raise RuntimeError(f"Expected non-empty audio/mpeg, got {content_type!r} ({len(response.content)} bytes)")
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(".part")
        partial.write_bytes(response.content)
        partial.replace(target)
        return response.headers.get("x-generation-id")
    raise RuntimeError("TTS retries exhausted")


def decode_source(source_mp3: Path, clean_wav: Path, sample_rate: int, force: bool) -> None:
    if clean_wav.exists() and clean_wav.stat().st_size > 2048 and not force:
        return
    clean_wav.parent.mkdir(parents=True, exist_ok=True)
    safe_run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source_mp3),
              "-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(clean_wav)])


def wav_read(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2:
            raise ValueError(f"Expected mono PCM16 WAV: {path}")
        rate = source.getframerate()
        audio = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    return audio, rate


def wav_write(path: Path, audio: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(pcm.tobytes())


def preprocess(clean_wav: Path, temp_wav: Path, profile: dict, sample_rate: int) -> None:
    filters: list[str] = []
    if profile["speed"] != 1.0:
        filters.append(f"atempo={profile['speed']}")
    if profile.get("bandpass_hz"):
        low, high = profile["bandpass_hz"]
        filters.extend([f"highpass=f={low}", f"lowpass=f={high}"])
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(clean_wav)]
    if filters:
        command.extend(["-af", ",".join(filters)])
    command.extend(["-ac", "1", "-ar", str(sample_rate), "-c:a", "pcm_s16le", str(temp_wav)])
    safe_run(command)


def augment(clean_wav: Path, target: Path, profile: dict, utterance_id: str, sample_rate: int, force: bool) -> None:
    if target.exists() and target.stat().st_size > 2048 and not force:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".working.wav")
    try:
        preprocess(clean_wav, temp, profile, sample_rate)
        audio, rate = wav_read(temp)
        delay_ms = profile.get("echo_delay_ms")
        if delay_ms:
            delay = round(rate * delay_ms / 1000)
            echoed = np.zeros_like(audio)
            if delay < len(audio):
                echoed[delay:] = audio[:-delay] * float(profile.get("echo_decay", 0.25))
            audio = audio + echoed
        gain = 10 ** (float(profile.get("gain_db", 0)) / 20)
        audio *= gain
        snr = profile.get("noise_snr_db")
        if snr is not None and audio.size:
            signal_rms = math.sqrt(float(np.mean(audio * audio)) + 1e-12)
            noise_rms = signal_rms / (10 ** (float(snr) / 20))
            seed = int(hashlib.sha256(f"{utterance_id}:{profile['id']}".encode()).hexdigest()[:16], 16)
            noise = np.random.default_rng(seed).normal(0, noise_rms, audio.size).astype(np.float32)
            audio += noise
        wav_write(target, audio, rate)
    finally:
        temp.unlink(missing_ok=True)


def duration(path: Path) -> float:
    with wave.open(str(path), "rb") as source:
        return round(source.getnframes() / source.getframerate(), 3)


def assigned_profiles(item: dict, profiles: list[dict]) -> list[dict]:
    if item["set"] == "floor_control":
        return profiles
    suffix = int(item["id"].rsplit("_", 1)[1])
    stress = {1: "fast", 2: "slow", 3: "quiet", 4: "noise_12db", 5: "room_echo", 6: "phone"}[suffix]
    return [profiles[0], next(profile for profile in profiles if profile["id"] == stress)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--control-grok", action="store_true", help="also generate floor-control anchors with five paid Grok voices")
    parser.add_argument("--limit", type=int, help="generate only the first N utterances")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    key = os.getenv("OPENROUTER_API_KEY")
    if not key and not args.dry_run:
        raise SystemExit("OPENROUTER_API_KEY is required")
    if not shutil_which("ffmpeg"):
        raise SystemExit("ffmpeg is required")

    utterances = read_jsonl(HERE / "utterances.jsonl")
    if args.limit:
        utterances = utterances[: args.limit]
    profile_data = json.loads((HERE / "profiles.json").read_text(encoding="utf-8"))
    profiles = profile_data["profiles"]
    sample_rate = int(profile_data["sample_rate"])
    engines = [FISH]
    if args.control_grok:
        engines.extend({"id": f"grok-{voice}", "model": "x-ai/grok-voice-tts-1.0", "voice": voice, "price_per_char": 0.000015} for voice in GROK_VOICES)
    estimated = sum(len(item["text"]) * engine["price_per_char"] for engine in engines for item in utterances
                    if engine is FISH or item["set"] == "floor_control")
    print(f"Utterances: {len(utterances)}; engines: {len(engines)}; paid upper bound: ${estimated:.4f}")
    if args.dry_run:
        return 0

    audio_root = HERE / "audio"
    records: list[dict] = []
    with httpx.Client(headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, timeout=90) as client:
        for engine in engines:
            selected = utterances if engine is FISH else [item for item in utterances if item["set"] == "floor_control"]
            for index, item in enumerate(selected, 1):
                label = f"{engine['id']}:{item['id']}"
                print(f"[{index}/{len(selected)}] {label}")
                source = audio_root / "_source" / engine["id"] / f"{item['id']}.mp3"
                generation_id = synthesize(client, engine, item["text"], source, args.force)
                clean = audio_root / engine["id"] / f"{item['id']}__clean.wav"
                decode_source(source, clean, sample_rate, args.force)
                variants = profiles if engine is not FISH else assigned_profiles(item, profiles)
                for profile in variants:
                    target = audio_root / engine["id"] / f"{item['id']}__{profile['id']}.wav"
                    if profile["id"] != "clean":
                        augment(clean, target, profile, item["id"], sample_rate, args.force)
                    else:
                        target = clean
                    records.append({
                        "audio": target.relative_to(HERE).as_posix(), "utterance_id": item["id"], "set": item["set"],
                        "text": item["text"], "decision": item["decision"], "replacement": item.get("replacement"),
                        "difficulty": item["difficulty"], "engine": engine["id"], "model": engine["model"],
                        "voice": engine.get("voice"), "profile": profile, "sample_rate": sample_rate,
                        "duration_sec": duration(target), "generation_id": generation_id,
                    })
    manifest = audio_root / "manifest.jsonl"
    manifest.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records), encoding="utf-8")
    print(f"Generated {len(records)} evaluation files; manifest: {manifest}")
    return 0


def shutil_which(name: str) -> str | None:
    from shutil import which
    return which(name)


if __name__ == "__main__":
    raise SystemExit(main())
