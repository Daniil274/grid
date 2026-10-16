"""Conversations kept one file each: compressed, encrypted, written in the background.

A single JSON file of every conversation costs a full rewrite of all of them on
each change - tens of megabytes once traces pile up - and the rewrite blocks
whoever saves. Here each conversation is a file of its own, written only when
it changed, by a writer thread: the caller hands over the conversation's JSON
and goes on.

A file is ``MAGIC`` + a flags byte + the body. The body is the conversation's
JSON, gzip-compressed; with a codec it is then sealed with AES-GCM under a key
derived from the server's secrets key (``GRID_SECRETS_KEY``), and starts with
the id of that key and the nonce. Rotating the secrets key keeps old files
readable: every key of the list opens, the first one seals.

``manifest.json`` holds what is not a conversation (the active one) and marks
the directory as complete: a store without it is not loaded.
"""

from __future__ import annotations

import atexit
import base64
import gzip
import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

logger = logging.getLogger("grid.conversation_store")

MAGIC = b"GRC1"
COMPRESSED = 0x01
SEALED = 0x02
SUFFIX = ".grc"
MANIFEST = "manifest.json"
_KEY_ID_BYTES = 8
_NONCE_BYTES = 12
#: Seconds between attempts while writing fails (a full disk, say).
RETRY_PAUSE = 2.0


class StoreError(RuntimeError):
    """A conversation file that cannot be read: damaged, or sealed with an unknown key."""


class KeyMissing(StoreError):
    """A sealed file whose key this server lacks: ``GRID_SECRETS_KEY`` is unset or
    names other keys. A configuration problem, not a damaged file - nothing is
    moved aside for it, and the store refuses to load until the key is back."""


class ConversationCodec:
    """AES-256-GCM over conversation files; the first key seals, every key opens."""

    def __init__(self, keys: Iterable[bytes]) -> None:
        self._keys: Dict[bytes, AESGCM] = {}
        self._sealing: Optional[bytes] = None
        for key in keys:
            key_id = hashlib.sha256(key).digest()[:_KEY_ID_BYTES]
            self._keys[key_id] = AESGCM(key)
            self._sealing = self._sealing or key_id
        if self._sealing is None:
            raise ValueError("A conversation codec needs at least one key")

    @classmethod
    def from_secrets(cls, raw: Optional[str]) -> Optional["ConversationCodec"]:
        """The codec of a comma-separated list of secrets keys; None when there is none.

        Each key (the vault's Fernet keys) yields its own AES key through HKDF,
        so the conversations never share a key with the stored credentials.
        """
        secrets = [part.strip() for part in (raw or "").split(",") if part.strip()]
        if not secrets:
            return None
        keys = []
        for secret in secrets:
            try:
                material = base64.urlsafe_b64decode(secret.encode())
            except (ValueError, TypeError):
                material = secret.encode()
            keys.append(
                HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"grid-conversations-v1").derive(material)
            )
        return cls(keys)

    def seal(self, data: bytes, binding: bytes) -> bytes:
        nonce = os.urandom(_NONCE_BYTES)
        return self._sealing + nonce + self._keys[self._sealing].encrypt(nonce, data, binding)

    def open(self, blob: bytes, binding: bytes) -> bytes:
        key_id, nonce, sealed = (
            blob[:_KEY_ID_BYTES],
            blob[_KEY_ID_BYTES:_KEY_ID_BYTES + _NONCE_BYTES],
            blob[_KEY_ID_BYTES + _NONCE_BYTES:],
        )
        aead = self._keys.get(key_id)
        if aead is None:
            raise KeyMissing("sealed with a key this server does not have")
        try:
            return aead.decrypt(nonce, sealed, binding)
        except Exception as exc:  # cryptography's InvalidTag says nothing more
            raise StoreError("damaged, or not this conversation's file") from exc


def pack(text: str, context_id: str, codec: Optional[ConversationCodec]) -> bytes:
    """A conversation's JSON as the bytes of its file."""
    body = gzip.compress(text.encode("utf-8"), compresslevel=5)
    if codec is None:
        return MAGIC + bytes([COMPRESSED]) + body
    # Bound to its conversation: a file copied under another id does not open.
    return MAGIC + bytes([COMPRESSED | SEALED]) + codec.seal(body, context_id.encode())


def unpack(blob: bytes, context_id: str, codec: Optional[ConversationCodec]) -> str:
    """The JSON of a conversation file."""
    if blob[:4] != MAGIC or len(blob) < 5:
        raise StoreError("not a conversation file")
    flags, body = blob[4], blob[5:]
    if flags & SEALED:
        if codec is None:
            raise KeyMissing("sealed, and no secrets key is set (GRID_SECRETS_KEY)")
        body = codec.open(body, context_id.encode())
    if flags & COMPRESSED:
        body = gzip.decompress(body)
    return body.decode("utf-8")


class ConversationStore:
    """A directory of conversation files and the thread that writes them."""

    def __init__(self, directory: Path, codec: Optional[ConversationCodec] = None) -> None:
        self.directory = Path(directory)
        self.codec = codec
        self._pending: Dict[str, Optional[str]] = {}  # id -> JSON to write, None to delete
        self._manifest: Optional[dict] = None
        self._busy = False
        self._failed = False
        self._condition = threading.Condition()
        self._thread: Optional[threading.Thread] = None
        self._closed = False

    # -- reading ---------------------------------------------------------------
    @property
    def exists(self) -> bool:
        return (self.directory / MANIFEST).exists()

    def path_of(self, context_id: str) -> Path:
        return self.directory / f"{context_id}{SUFFIX}"

    def read_all(self) -> Tuple[dict, Dict[str, str]]:
        """(manifest, {id: JSON}) of the stored conversations. A damaged file is
        moved aside and logged, never silently dropped.

        A file sealed with a key the server lacks raises :class:`KeyMissing`
        before anything is moved: a missing or wrong ``GRID_SECRETS_KEY`` must
        stop the start, not empty every chat list behind ``.unreadable`` files.
        """
        manifest = json.loads((self.directory / MANIFEST).read_text(encoding="utf-8"))
        found: Dict[str, str] = {}
        damaged = []
        locked = []
        for path in sorted(self.directory.glob(f"*{SUFFIX}")):
            context_id = path.name[: -len(SUFFIX)]
            try:
                found[context_id] = unpack(path.read_bytes(), context_id, self.codec)
            except KeyMissing:
                locked.append(path.name)
            except (StoreError, OSError, EOFError, ValueError) as exc:
                damaged.append((path, context_id, exc))
        if locked:
            raise KeyMissing(
                f"{len(locked)} conversation file(s) in {self.directory} are sealed with a key this "
                f"server does not have (first: {locked[0]}). Set GRID_SECRETS_KEY to the key they were "
                "written with - a rotated key goes after the new one, comma-separated."
            )
        for path, context_id, exc in damaged:
            kept = path.with_name(path.name + ".unreadable")
            logger.error("Conversation %s cannot be read (%s); kept as %s", context_id, exc, kept.name)
            try:
                os.replace(path, kept)
            except OSError:
                logger.error("Could not move %s aside", path)
        return manifest, found

    # -- writing ---------------------------------------------------------------
    def put(self, context_id: str, text: str) -> None:
        """Write a conversation's JSON soon; a newer put of it replaces this one."""
        self._queue(context_id, text)

    def remove(self, context_id: str) -> None:
        self._queue(context_id, None)

    def set_manifest(self, manifest: dict) -> None:
        with self._condition:
            self._manifest = dict(manifest)
            self._start()
            self._condition.notify()

    def flush(self, *, durable: bool = False, timeout: float = 60.0) -> bool:
        """Wait until everything handed over is on disk; ``durable`` syncs the
        directory too. False when the writer failed or did not finish in time."""
        with self._condition:
            done = self._condition.wait_for(
                lambda: not self._pending and self._manifest is None and not self._busy, timeout=timeout
            )
            failed, self._failed = self._failed, False
        if done and durable and os.name == "posix":
            descriptor = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return done and not failed

    def close(self) -> None:
        self.flush()
        with self._condition:
            self._closed = True
            self._condition.notify()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _queue(self, context_id: str, text: Optional[str]) -> None:
        with self._condition:
            self._pending[context_id] = text
            self._start()
            self._condition.notify()

    def _start(self) -> None:
        if self._thread is None:
            # A daemon thread dies with the process: what it holds is written first.
            atexit.register(self.flush, timeout=10.0)
        if self._thread is None or not self._thread.is_alive():
            self._closed = False
            self._thread = threading.Thread(target=self._run, name="conversation-writer", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._pending or self._manifest is not None or self._closed)
                if self._closed and not self._pending and self._manifest is None:
                    return
                batch, self._pending = self._pending, {}
                manifest, self._manifest = self._manifest, None
                self._busy = True
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                for context_id, text in batch.items():
                    if text is None:
                        self.path_of(context_id).unlink(missing_ok=True)
                    else:
                        self._write(self.path_of(context_id), pack(text, context_id, self.codec))
                if manifest is not None:
                    self._write(self.directory / MANIFEST, json.dumps(manifest).encode("utf-8"))
            except Exception:
                logger.exception("Writing conversations to %s failed", self.directory)
                with self._condition:
                    self._failed = True
                    # Not lost: what failed is written again with the next change.
                    for context_id, text in batch.items():
                        self._pending.setdefault(context_id, text)
                    if manifest is not None and self._manifest is None:
                        self._manifest = manifest
            finally:
                with self._condition:
                    self._busy = False
                    self._condition.notify_all()
            if self._failed:
                time.sleep(RETRY_PAUSE)

    @staticmethod
    def _write(path: Path, data: bytes) -> None:
        partial = path.with_name(path.name + ".tmp")
        with open(partial, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(partial, path)


def export_legacy(directory: Path, target: Path, codec: Optional[ConversationCodec]) -> int:
    """Write the store's conversations as the old single file (``conversations.json``).

    The way back to a build without the store: that build reads *target* where
    it kept its conversations. Returns how many conversations were written.
    """
    store = ConversationStore(Path(directory), codec)
    if not store.exists:
        raise StoreError(f"{directory} holds no conversation store ({MANIFEST} is missing)")
    manifest, found = store.read_all()
    parts = [json.dumps(context_id) + ":" + text for context_id, text in found.items()]
    document = (
        '{"active_context_id":' + json.dumps(manifest.get("active_context_id"))
        + ',"contexts":{' + ",".join(parts) + "}}"
    )
    ConversationStore._write(Path(target), document.encode("utf-8"))
    return len(found)


def main(argv: Optional[list] = None) -> int:
    """``python -m core.conversation_store export <store dir> <conversations.json>``."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m core.conversation_store")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="write the store back as the old single conversations file")
    export.add_argument("directory", type=Path)
    export.add_argument("target", type=Path)
    args = parser.parse_args(argv)
    if args.target.exists():
        parser.error(f"{args.target} exists; move it aside first")
    count = export_legacy(args.directory, args.target, ConversationCodec.from_secrets(os.environ.get("GRID_SECRETS_KEY")))
    print(f"Wrote {count} conversation(s) to {args.target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
