#!/usr/bin/env python3
"""Live English -> Traditional Chinese subtitles for online meetings.

Pipeline (standard library only, no third-party Python packages):

    ffmpeg (loopback device such as BlackHole, or an audio file)
      -> 16 kHz mono PCM -> energy-based VAD segmentation
      -> whisper.cpp server  (speech recognition, English)
      -> Ollama              (translation to Traditional Chinese)
      -> terminal + browser overlay (SSE) + transcript files (.jsonl / .txt)

Run `./run.sh start` to launch everything, or `python3 live_subs.py --help`.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from array import array
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SAMPLE_RATE = 16000
FRAME_MS = 20
FRAME_BYTES = SAMPLE_RATE * FRAME_MS // 1000 * 2  # 16-bit mono
HERE = Path(__file__).resolve().parent

DEFAULT_OLLAMA_MODEL = "qwen3:8b"  # override with OLLAMA_MODEL (see config.env.example)

SYSTEM_PROMPT = """You are a simultaneous interpreter for a live online engineering meeting.
Translate each English utterance you receive into natural, fluent Traditional Chinese as used in Taiwan (繁體中文、台灣用語).

Rules:
- Output ONLY the translation. No explanations, no quotes, no labels, no pinyin, no notes.
- Keep product names, company names, acronyms, code identifiers, file names, version numbers and units in their original form (e.g. GPU, CUDA, API, Q1, v2.3, Teams).
- The input comes from live speech recognition: it may be a fragment, lack punctuation, or contain small recognition errors. Translate what is there as-is; never complete it, answer it, or comment on it.
- If the utterance is a question or a request, translate it as a question or request; never answer it.
- Use Taiwan terminology, e.g. 軟體、程式、記憶體、伺服器、資料、網路、影片、專案、品質、支援、預設、使用者。
"""

# Prompt formats for translation-specialised models: one user message, no system prompt, no history.
TRANSLATEGEMMA_TEMPLATE = (
    "You are a professional English (en) to Traditional Chinese (zh-Hant) translator. "
    "Your goal is to accurately convey the meaning and nuances of the original English text while adhering to "
    "Traditional Chinese grammar, vocabulary, and cultural sensitivities. Produce only the Traditional Chinese "
    "translation, without any additional explanations or commentary. Please translate the following English text "
    "into Traditional Chinese:\n\n\n{text}"
)
# Hy-MT2 prompt pieces (from the model card). English instruction tested best on the 7B; the 1.8B keeps
# Traditional script more reliably with the Chinese one (--mt-prompt-lang zh).
HYMT_INSTRUCTION = {
    "en": "Translate the following text into Traditional Chinese. Note that you should only output the translated "
          "result without any additional explanation:\n\n{text}",
    "zh": "将以下文本翻译成繁体中文，注意只需要输出翻译后的结果，不要额外解释：\n\n{text}",
}
HYMT_REFERENCE_HEADER = "Reference the following translations:\n"
PROMPT_STYLES = ("auto", "chat", "translategemma", "hymt")


def detect_prompt_style(model: str, requested: str) -> str:
    if requested != "auto":
        return requested
    name = model.lower()
    if "translategemma" in name:
        return "translategemma"
    if "hy-mt" in name or "hymt" in name or "hunyuan-mt" in name:
        return "hymt"
    return "chat"


# Typical Whisper hallucinations on near-silent audio (normalized: lowercase, alnum + spaces).
HALLUCINATIONS = {
    "thank you", "thanks for watching", "thank you for watching", "thank you very much",
    "you", "bye", "the end", "see you next time", "subtitles by the amaraorg community",
    "thanks for listening", "please subscribe", "so", "oh", "um", "hmm", "uh",
}

ANSI_DIM = "\033[2m"
ANSI_BOLD = "\033[1m"
ANSI_RESET = "\033[0m"
ANSI_RED = "\033[31m"


# --------------------------------------------------------------------------- data


@dataclass
class Segment:
    start: float          # seconds since capture start
    end: float
    pcm: bytes
    speech_ms: int


@dataclass
class Item:
    id: int
    seg: Segment
    en: str
    wall: datetime
    asr_ms: float
    zh: str = ""
    tr_ms: float = 0.0
    tr_first_ms: float = 0.0
    meta: dict = field(default_factory=dict)


def format_stats(seg_s: float, asr_ms: float, tr_ms: float | None, tr_first_ms: float | None) -> str:
    """Timing summary shown under each sentence, e.g. 'seg 2.8s · asr 0.5s · 翻譯 0.6s (首字 0.2s)'."""
    stats = f"seg {seg_s:.1f}s · asr {asr_ms / 1000:.1f}s"
    if tr_ms is not None:
        stats += f" · 翻譯 {tr_ms / 1000:.1f}s"
        if tr_first_ms is not None:
            stats += f" (首字 {tr_first_ms / 1000:.1f}s)"
    return stats


def format_transcript_entry(time_str: str, en: str, zh: str, seg_s: float, asr_ms: float,
                            tr_ms: float | None, tr_first_ms: float | None) -> str:
    """One transcript block in the same layout as the terminal output:

        [HH:MM:SS] English sentence
        → 中文翻譯
           seg 2.8s · asr 0.5s · 翻譯 0.6s (首字 0.2s)
    """
    lines = [f"[{time_str}] {en}"]
    if zh:
        lines.append(f"→ {zh}")
    lines.append(f"   {format_stats(seg_s, asr_ms, tr_ms, tr_first_ms)}")
    return "\n".join(lines) + "\n\n"


# --------------------------------------------------------------------------- audio


def list_avfoundation_devices() -> list[tuple[int, str]]:
    """Return [(index, name)] of AVFoundation audio input devices as seen by ffmpeg."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    out = proc.stderr + proc.stdout
    devices: list[tuple[int, str]] = []
    in_audio = False
    for line in out.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        if in_audio:
            m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
            if m:
                devices.append((int(m.group(1)), m.group(2)))
    return devices


def pick_device(requested: str | None) -> str:
    """Resolve the ffmpeg avfoundation audio spec (":index" or ":name")."""
    devices = list_avfoundation_devices()
    if requested:
        if requested.isdigit():
            return f":{requested}"
        for idx, name in devices:
            if requested.lower() in name.lower():
                print(f"Using audio device [{idx}] {name}")
                return f":{idx}"
        sys.exit(f"Audio device not found: {requested!r}. Available: {devices}")
    for idx, name in devices:
        if "blackhole" in name.lower():
            print(f"Using loopback device [{idx}] {name}")
            return f":{idx}"
    if devices:
        idx, name = devices[0]
        print(f"{ANSI_RED}No BlackHole device found; falling back to [{idx}] {name} (microphone).{ANSI_RESET}")
        return f":{idx}"
    sys.exit("ffmpeg found no AVFoundation audio devices.")


def start_ffmpeg(device_spec: str | None, file_path: str | None, realtime: bool) -> subprocess.Popen:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
    if file_path:
        if realtime:
            cmd += ["-re"]
        cmd += ["-i", file_path]
    else:
        cmd += ["-f", "avfoundation", "-i", device_spec or ":0"]
    cmd += ["-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "pipe:1"]
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)


def frames_from(proc: subprocess.Popen, stop: threading.Event):
    """Yield fixed-size PCM frames from the ffmpeg stdout pipe."""
    assert proc.stdout is not None
    buf = b""
    while not stop.is_set():
        chunk = proc.stdout.read(FRAME_BYTES - len(buf) if len(buf) < FRAME_BYTES else FRAME_BYTES)
        if not chunk:
            break
        buf += chunk
        while len(buf) >= FRAME_BYTES:
            yield buf[:FRAME_BYTES]
            buf = buf[FRAME_BYTES:]


def frame_db(frame: bytes) -> float:
    samples = array("h")
    samples.frombytes(frame)
    if not samples:
        return -100.0
    acc = 0
    for s in samples:
        acc += s * s
    rms = math.sqrt(acc / len(samples))
    return 20.0 * math.log10(max(rms, 1.0) / 32768.0)


def pcm_to_wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


# --------------------------------------------------------------------------- VAD


class Segmenter:
    """Energy-based voice activity segmentation with an adaptive noise floor."""

    def __init__(self, a: argparse.Namespace):
        self.threshold_db = a.threshold_db
        self.start_frames = 3
        self.silence_ms = a.silence_ms
        self.min_speech_ms = a.min_speech_ms
        self.max_seg_ms = int(a.max_seg_s * 1000)
        self.soft_cut_ms = int(a.soft_cut_s * 1000)
        self.soft_silence_ms = a.soft_silence_ms
        self.post_roll_frames = max(1, a.post_roll_ms // FRAME_MS)
        self.pre = deque(maxlen=max(1, a.pre_roll_ms // FRAME_MS))
        self.buf: list[bytes] = []
        self.in_speech = False
        self.speech_frames = 0
        self.trailing_silence = 0
        self.consec_speech = 0
        self.start_t = 0.0
        self.t = 0.0
        self.noise_db = -70.0
        self.last_db = -100.0

    def effective_threshold(self) -> float:
        return max(self.threshold_db, self.noise_db + 15.0)

    def push(self, frame: bytes) -> Segment | None:
        db = frame_db(frame)
        self.last_db = db
        if db < self.noise_db + 6.0:
            self.noise_db = 0.98 * self.noise_db + 0.02 * db
        else:
            self.noise_db = min(self.noise_db + 0.003, -30.0)
        is_speech = db > self.effective_threshold()
        t0 = self.t
        self.t += FRAME_MS / 1000.0

        if not self.in_speech:
            self.pre.append(frame)
            if is_speech:
                self.consec_speech += 1
                if self.consec_speech >= self.start_frames:
                    self.in_speech = True
                    self.buf = list(self.pre)
                    self.start_t = max(0.0, t0 - (len(self.pre) - 1) * FRAME_MS / 1000.0)
                    self.speech_frames = self.consec_speech
                    self.trailing_silence = 0
            else:
                self.consec_speech = 0
            return None

        self.buf.append(frame)
        if is_speech:
            self.speech_frames += 1
            self.trailing_silence = 0
        else:
            self.trailing_silence += 1
        dur_ms = len(self.buf) * FRAME_MS
        sil_ms = self.trailing_silence * FRAME_MS
        if (
            sil_ms >= self.silence_ms
            or (dur_ms >= self.soft_cut_ms and sil_ms >= self.soft_silence_ms)
            or dur_ms >= self.max_seg_ms
        ):
            return self._finalize()
        return None

    def flush(self) -> Segment | None:
        return self._finalize() if self.in_speech else None

    def _finalize(self) -> Segment | None:
        drop = max(0, self.trailing_silence - self.post_roll_frames)
        frames = self.buf[: len(self.buf) - drop] if drop else self.buf
        seg = None
        if self.speech_frames * FRAME_MS >= self.min_speech_ms and frames:
            seg = Segment(
                start=self.start_t,
                end=self.start_t + len(frames) * FRAME_MS / 1000.0,
                pcm=b"".join(frames),
                speech_ms=self.speech_frames * FRAME_MS,
            )
        self.in_speech = False
        self.buf = []
        self.speech_frames = 0
        self.trailing_silence = 0
        self.consec_speech = 0
        self.pre.clear()
        return seg


# --------------------------------------------------------------------------- whisper.cpp


def multipart(fields: dict[str, str], file_field: str, filename: str, data: bytes, ctype: str):
    boundary = "----LiveSubs" + uuid.uuid4().hex
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    out.write(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {ctype}\r\n\r\n".encode()
    )
    out.write(data)
    out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"


class WhisperClient:
    def __init__(self, base_url: str, prompt: str = "", no_speech_thold: float = 0.7):
        self.url = base_url.rstrip("/") + "/inference"
        self.prompt = prompt
        self.no_speech_thold = no_speech_thold

    def transcribe(self, pcm: bytes) -> tuple[str, dict]:
        fields = {
            "temperature": "0.0",
            "temperature_inc": "0.2",
            "response_format": "verbose_json",
            "language": "en",
        }
        if self.prompt:
            fields["prompt"] = self.prompt
        body, ctype = multipart(fields, "file", "segment.wav", pcm_to_wav(pcm), "audio/wav")
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": ctype})
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode("utf-8", "replace").strip(), {}
        segments = data.get("segments")
        if isinstance(segments, list) and segments:
            kept = []
            dropped = 0
            for s in segments:
                txt = (s.get("text") or "").strip()
                if not txt:
                    continue
                if float(s.get("no_speech_prob") or 0.0) > self.no_speech_thold:
                    dropped += 1
                    continue
                kept.append(txt)
            return " ".join(kept), {"segments": len(segments), "dropped": dropped}
        return (data.get("text") or "").strip(), {}


def clean_en(text: str) -> str:
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|<[^>]*>", " ", text)  # [MUSIC], (laughs), <|nospeech|>
    text = re.sub(r"\s+", " ", text).strip()
    return text


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def is_hallucination(text: str, seg: Segment, prev_norm: str) -> bool:
    n = normalize(text)
    if not n:
        return True
    if n in HALLUCINATIONS and seg.speech_ms < 2500:
        return True
    if prev_norm and n == prev_norm and seg.speech_ms < 2500:
        return True
    words = n.split()
    if len(words) >= 12 and len(set(words)) <= len(words) / 4:
        return True  # degenerate repetition
    return False


# --------------------------------------------------------------------------- Ollama


class OllamaTranslator:
    def __init__(self, base_url: str, model: str, system_prompt: str, style: str = "chat", keep_alive: str = "3h",
                 glossary: list[tuple[str, str]] | None = None, mt_prompt_lang: str = "en"):
        self.url = base_url.rstrip("/") + "/api/chat"
        self.model = model
        self.system_prompt = system_prompt
        self.style = style
        self.glossary = glossary or []
        self.mt_prompt_lang = mt_prompt_lang if mt_prompt_lang in HYMT_INSTRUCTION else "en"
        self.keep_alive = keep_alive
        self.use_think_flag = style == "chat"  # Qwen3-style thinking switch; MT models have no thinking mode
        self.options: dict = {"temperature": 0.2, "num_predict": 300}
        if style == "hymt":  # Hy-MT2 card recommends temp 0.7; 0.2 was more faithful in our meeting-sentence tests
            self.options = {"temperature": 0.2, "top_p": 0.6, "top_k": 20, "repeat_penalty": 1.05, "num_predict": 300}

    def build_messages(self, text: str, history: list[tuple[str, str]]) -> list[dict]:
        if self.style == "translategemma":
            return [{"role": "user", "content": TRANSLATEGEMMA_TEMPLATE.format(text=text)}]
        if self.style == "hymt":
            prompt = ""
            if self.glossary:  # terminology template from the Hy-MT2 model card
                lines = "\n".join(f"{en} translates to {zh or en}" for en, zh in self.glossary[:30])
                prompt = HYMT_REFERENCE_HEADER + lines + "\n\n"
            prompt += HYMT_INSTRUCTION[self.mt_prompt_lang].format(text=text)
            return [{"role": "user", "content": prompt}]
        messages = [{"role": "system", "content": self.system_prompt}]
        for en, zh in history:
            messages.append({"role": "user", "content": en})
            messages.append({"role": "assistant", "content": zh})
        messages.append({"role": "user", "content": text})
        return messages

    def _request(self, messages: list[dict], think_flag: bool):
        body = {
            "model": self.model,
            "stream": True,
            "keep_alive": self.keep_alive,
            "messages": messages,
            "options": dict(self.options),
        }
        if think_flag:
            body["think"] = False
        req = urllib.request.Request(
            self.url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        return urllib.request.urlopen(req, timeout=180)

    def translate(self, text: str, history: list[tuple[str, str]]):
        """Yield ("delta", str) chunks, then a final ("stats", dict)."""
        messages = self.build_messages(text, history)
        try:
            resp = self._request(messages, self.use_think_flag)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if self.use_think_flag and "think" in detail.lower():
                self.use_think_flag = False
                resp = self._request(messages, False)
            else:
                raise RuntimeError(f"Ollama HTTP {e.code}: {detail[:200]}") from e
        with resp:
            for line in resp:
                if not line.strip():
                    continue
                d = json.loads(line)
                if d.get("error"):
                    raise RuntimeError(f"Ollama error: {d['error']}")
                delta = (d.get("message") or {}).get("content") or ""
                if delta:
                    yield ("delta", delta)
                if d.get("done"):
                    yield ("stats", {
                        "prompt_tokens": d.get("prompt_eval_count"),
                        "eval_tokens": d.get("eval_count"),
                        "eval_s": (d.get("eval_duration") or 0) / 1e9,
                    })


def opencc_convert(text: str, config: str) -> str:
    """Convert with the OpenCC CLI (brew install opencc); returns the input unchanged on any failure."""
    if not text or config == "off":
        return text
    try:
        proc = subprocess.run(["opencc", "-c", f"{config}.json"], input=text, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return text
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else text


def clean_zh(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = text.strip()
    text = re.sub(r"^(翻譯|譯文|中文|Translation)\s*[:：]\s*", "", text)
    if len(text) >= 2 and text[0] in "「“\"'『" and text[-1] in "」”\"'』":
        text = text[1:-1]
    return re.sub(r"[ \t]+", " ", text).strip()


# --------------------------------------------------------------------------- output


class Broadcaster:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients: set[queue.Queue] = set()
        self.items: dict[int, dict] = {}
        self.order: deque[int] = deque(maxlen=60)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self.lock:
            self.clients.add(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        with self.lock:
            self.clients.discard(q)

    def publish(self, ev: dict):
        data = json.dumps(ev, ensure_ascii=False)
        with self.lock:
            iid = ev.get("id")
            if iid is not None:
                if iid not in self.items:
                    self.items[iid] = {"id": iid, "t": ev.get("t", ""), "en": "", "zh": "", "done": False}
                    self.order.append(iid)
                    for old in list(self.items):
                        if old not in self.order:
                            del self.items[old]
                it = self.items[iid]
                if ev["type"] == "en":
                    it["en"] = ev["text"]
                elif ev["type"] == "zh":
                    it["zh"] = ev["text"]
                    it["done"] = bool(ev.get("done"))
            for q in list(self.clients):
                try:
                    q.put_nowait(data)
                except queue.Full:
                    pass

    def state(self) -> list[dict]:
        with self.lock:
            return [self.items[i] for i in list(self.order)[-12:] if i in self.items]


def make_handler(bc: Broadcaster, overlay_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # silence request logging
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/":
                try:
                    html = overlay_path.read_bytes()
                except OSError:
                    html = b"<h1>overlay.html missing</h1>"
                self._send(200, html, "text/html; charset=utf-8")
            elif path == "/state":
                self._send(200, json.dumps(bc.state(), ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                q = bc.subscribe()
                try:
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while True:
                        try:
                            data = q.get(timeout=15)
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            continue
                        self.wfile.write(f"data: {data}\n\n".encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    bc.unsubscribe(q)
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


class Display:
    def __init__(self, bc: Broadcaster | None, out_dir: Path, translate: bool):
        self.bc = bc
        self.translate = translate
        self.lock = threading.Lock()
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        self.jsonl_path = out_dir / f"{stamp}.jsonl"
        self.txt_path = out_dir / f"{stamp}.txt"
        self.jsonl = open(self.jsonl_path, "a", encoding="utf-8")
        self.txt = open(self.txt_path, "a", encoding="utf-8")

    def begin(self, item: Item):
        t = item.wall.strftime("%H:%M:%S")
        with self.lock:
            sys.stdout.write(f"\n{ANSI_DIM}[{t}] {item.en}{ANSI_RESET}\n")
            if self.translate:
                sys.stdout.write(f"{ANSI_BOLD}→ ")
            sys.stdout.flush()
        if self.bc:
            self.bc.publish({"type": "en", "id": item.id, "t": t, "text": item.en})
            if self.translate:
                self.bc.publish({"type": "zh", "id": item.id, "text": "", "done": False})

    def partial(self, item: Item, delta: str, full: str):
        with self.lock:
            sys.stdout.write(delta)
            sys.stdout.flush()
        if self.bc:
            self.bc.publish({"type": "zh", "id": item.id, "text": full, "done": False})

    def end(self, item: Item, stats_line: str):
        with self.lock:
            if self.translate:
                sys.stdout.write(f"{ANSI_RESET}\n")
                if item.meta.get("converted"):
                    sys.stdout.write(f"   ⇒ {ANSI_BOLD}{item.zh}{ANSI_RESET}\n")
            if stats_line:
                sys.stdout.write(f"{ANSI_DIM}   {stats_line}{ANSI_RESET}\n")
            sys.stdout.flush()
        if self.bc and self.translate:
            self.bc.publish({"type": "zh", "id": item.id, "text": item.zh, "done": True})
        tr_ms = round(item.tr_ms) if self.translate else None
        tr_first_ms = round(item.tr_first_ms) if self.translate else None
        rec = {
            "id": item.id,
            "time": item.wall.isoformat(timespec="seconds"),
            "start": round(item.seg.start, 2),
            "end": round(item.seg.end, 2),
            "en": item.en,
            "zh": item.zh,
            "asr_ms": round(item.asr_ms),
            "tr_ms": tr_ms,
            "tr_first_ms": tr_first_ms,
        }
        self.jsonl.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.jsonl.flush()
        self.txt.write(format_transcript_entry(item.wall.strftime("%H:%M:%S"), item.en, item.zh,
                                               item.seg.end - item.seg.start, item.asr_ms, tr_ms, tr_first_ms))
        self.txt.flush()

    def error(self, msg: str):
        with self.lock:
            sys.stdout.write(f"{ANSI_RESET}\n{ANSI_RED}!! {msg}{ANSI_RESET}\n")
            sys.stdout.flush()

    def close(self):
        self.jsonl.close()
        self.txt.close()


# --------------------------------------------------------------------------- helpers


def wait_for_port(url: str, name: str, timeout_s: float = 60.0) -> bool:
    m = re.match(r"https?://([^:/]+):(\d+)", url)
    host, port = (m.group(1), int(m.group(2))) if m else ("127.0.0.1", 80)
    deadline = time.time() + timeout_s
    announced = False
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            if not announced:
                print(f"Waiting for {name} at {host}:{port} ...")
                announced = True
            time.sleep(0.5)
    return False


def load_glossary(path: Path) -> tuple[str, str, list[tuple[str, str]]]:
    """Return (whisper_prompt, glossary_block_for_chat_llm, [(en, zh_or_empty), ...])."""
    if not path.exists():
        example = path.with_name("glossary.example.txt")
        if not example.exists():
            return "", "", []
        path = example
    terms = []
    pairs = []
    entries: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            en, zh = (p.strip() for p in line.split("=", 1))
        else:
            en, zh = line, ""
        if en:
            terms.append(en)
            entries.append((en, zh))
            pairs.append(f"- {en}" + (f" → {zh}" if zh else " (keep in English)"))
    whisper_prompt = ", ".join(terms)[:400]
    block = ("\nGlossary (follow these renderings):\n" + "\n".join(pairs) + "\n") if pairs else ""
    return whisper_prompt, block, entries


# --------------------------------------------------------------------------- main


def load_config_env(path: Path) -> None:
    """Apply KEY=VALUE lines from config.env as environment defaults (run.sh does the same for shell users)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        value = value.strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value


def parse_args() -> argparse.Namespace:
    load_config_env(HERE / "config.env")
    p = argparse.ArgumentParser(description="Live EN -> zh-TW subtitles (ffmpeg + whisper.cpp + Ollama).")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--device", help="AVFoundation audio device name (substring) or index; default: BlackHole")
    src.add_argument("--file", help="Audio/video file instead of a live device (for testing)")
    p.add_argument("--list-devices", action="store_true", help="List audio devices and exit")
    p.add_argument("--fast", action="store_true", help="With --file: decode faster than real time")
    whisper_default = os.environ.get("WHISPER_URL") or f"http://127.0.0.1:{os.environ.get('WHISPER_PORT', '8178')}"
    p.add_argument("--whisper-url", default=whisper_default)
    p.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"))
    p.add_argument("--model", default=os.environ.get("OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL))
    p.add_argument("--no-translate", action="store_true", help="English captions only")
    p.add_argument("--prompt-style", choices=PROMPT_STYLES, default="auto",
                   help="Prompt format: chat (general LLM), translategemma, hymt (Hy-MT2); auto = pick from the model name")
    p.add_argument("--mt-prompt-lang", choices=("en", "zh"), default="en",
                   help="Instruction language for the hymt style (en tested best on Hy-MT2 7B; zh keeps the 1.8B in Traditional script)")
    p.add_argument("--opencc", default="auto",
                   help="OpenCC post-conversion: off, s2t, s2tw, s2twp, or auto (s2t for MT-specialised models when opencc is installed)")
    p.add_argument("--context-pairs", type=int, default=1, help="Previous EN/ZH pairs given to the LLM as context")
    p.add_argument("--glossary", default=str(HERE / "glossary.txt"))
    p.add_argument("--out-dir", default=str(HERE / "transcripts"))
    p.add_argument("--web-port", type=int, default=8765)
    p.add_argument("--no-web", action="store_true", help="Disable the browser overlay server")
    p.add_argument("--no-open", action="store_true", help="Do not open the overlay in the browser")
    # VAD tuning
    p.add_argument("--threshold-db", type=float, default=-60.0,
                   help="Minimum speech level in dBFS (default -60). A Multi-Output Device scales the BlackHole copy by the "
                        "speaker volume, so keep this low; the adaptive noise floor (+15 dB) protects microphone input")
    p.add_argument("--silence-ms", type=int, default=700, help="Silence that ends a segment (default 700)")
    p.add_argument("--min-speech-ms", type=int, default=400)
    p.add_argument("--max-seg-s", type=float, default=12.0, help="Hard cut length (default 12 s)")
    p.add_argument("--soft-cut-s", type=float, default=7.0, help="After this length, cut on a short pause")
    p.add_argument("--soft-silence-ms", type=int, default=300)
    p.add_argument("--pre-roll-ms", type=int, default=240)
    p.add_argument("--post-roll-ms", type=int, default=200)
    p.add_argument("--no-speech-thold", type=float, default=0.7)
    p.add_argument("--vu", action="store_true", help="Print a level meter while idle (for threshold tuning)")
    return p.parse_args()


def main() -> int:
    a = parse_args()
    if a.list_devices:
        for idx, name in list_avfoundation_devices():
            print(f"[{idx}] {name}")
        return 0

    translate = not a.no_translate
    if not wait_for_port(a.whisper_url, "whisper-server"):
        print(f"{ANSI_RED}whisper-server not reachable at {a.whisper_url}. Start it with ./run.sh start{ANSI_RESET}")
        return 1
    if translate and not wait_for_port(a.ollama_url, "Ollama", timeout_s=15):
        print(f"{ANSI_RED}Ollama not reachable at {a.ollama_url}.{ANSI_RESET}")
        return 1

    whisper_prompt, glossary_block, glossary_entries = load_glossary(Path(a.glossary))
    whisper = WhisperClient(a.whisper_url, prompt=whisper_prompt, no_speech_thold=a.no_speech_thold)
    style = detect_prompt_style(a.model, a.prompt_style)
    opencc_cfg = a.opencc
    if opencc_cfg == "auto":
        opencc_cfg = "s2t" if (style != "chat" and shutil.which("opencc")) else "off"
    elif opencc_cfg != "off" and not shutil.which("opencc"):
        print(f"{ANSI_RED}opencc not found (brew install opencc); conversion disabled{ANSI_RESET}")
        opencc_cfg = "off"
    translator = (
        OllamaTranslator(a.ollama_url, a.model, SYSTEM_PROMPT + glossary_block, style=style,
                         glossary=glossary_entries, mt_prompt_lang=a.mt_prompt_lang)
        if translate else None
    )
    if translator:
        print(f"Translator: {a.model}  (prompt style: {style}, opencc: {opencc_cfg})")

    bc = None
    httpd = None
    if not a.no_web:
        bc = Broadcaster()
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", a.web_port), make_handler(bc, HERE / "overlay.html"))
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{a.web_port}/"
            print(f"Overlay: {url}")
            if not a.no_open:
                subprocess.Popen(["open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            print(f"{ANSI_RED}Overlay server disabled: {e}{ANSI_RESET}")
            bc = None

    display = Display(bc, Path(a.out_dir), translate)
    print(f"Transcript: {display.txt_path}")

    device_spec = None if a.file else pick_device(a.device)
    ffmpeg = start_ffmpeg(device_spec, a.file, realtime=not a.fast)
    stop = threading.Event()
    seg_q: queue.Queue = queue.Queue()
    tr_q: queue.Queue = queue.Queue()
    counter = {"n": 0}

    def capture():
        segmenter = Segmenter(a)
        last_vu = 0.0
        try:
            for frame in frames_from(ffmpeg, stop):
                seg = segmenter.push(frame)
                if seg:
                    seg_q.put(seg)
                    backlog = seg_q.qsize()
                    if backlog >= 4:
                        display.error(f"ASR backlog: {backlog} segments waiting")
                elif a.vu and not segmenter.in_speech and time.time() - last_vu > 0.5:
                    last_vu = time.time()
                    with display.lock:
                        sys.stdout.write(
                            f"\r{ANSI_DIM}level {segmenter.last_db:6.1f} dB  threshold {segmenter.effective_threshold():6.1f} dB{ANSI_RESET}   "
                        )
                        sys.stdout.flush()
            seg = segmenter.flush()
            if seg:
                seg_q.put(seg)
        finally:
            seg_q.put(None)
            if not stop.is_set() and ffmpeg.poll() not in (None, 0) and ffmpeg.stderr is not None:
                err = ffmpeg.stderr.read().decode("utf-8", "replace").strip()
                if err:
                    display.error(f"ffmpeg: {err[-500:]}")

    def asr_worker():
        prev_norm = ""
        while True:
            seg = seg_q.get()
            if seg is None:
                tr_q.put(None)
                return
            t0 = time.time()
            try:
                text, meta = whisper.transcribe(seg.pcm)
            except Exception as e:  # keep the stream alive on single failures
                display.error(f"whisper: {e}")
                continue
            asr_ms = (time.time() - t0) * 1000
            text = clean_en(text)
            if not text or is_hallucination(text, seg, prev_norm):
                continue
            prev_norm = normalize(text)
            counter["n"] += 1
            tr_q.put(Item(id=counter["n"], seg=seg, en=text, wall=datetime.now(), asr_ms=asr_ms, meta=meta))

    def tr_worker():
        history: deque[tuple[str, str]] = deque(maxlen=max(0, a.context_pairs))
        while True:
            item = tr_q.get()
            if item is None:
                return
            display.begin(item)
            seg_s = item.seg.end - item.seg.start
            stats_line = format_stats(seg_s, item.asr_ms, None, None)
            if translator:
                parts: list[str] = []
                t0 = time.time()
                first = None
                try:
                    for kind, payload in translator.translate(item.en, list(history)):
                        if kind == "delta":
                            if first is None:
                                first = time.time()
                            parts.append(payload)
                            display.partial(item, payload, "".join(parts))
                        else:
                            item.meta.update(payload)
                except Exception as e:
                    display.error(f"translate: {e}")
                item.tr_ms = (time.time() - t0) * 1000
                item.tr_first_ms = ((first or time.time()) - t0) * 1000
                item.zh = clean_zh("".join(parts))
                if opencc_cfg != "off":
                    converted = opencc_convert(item.zh, opencc_cfg)
                    if converted != item.zh:
                        item.zh = converted
                        item.meta["converted"] = True
                if item.zh:
                    history.append((item.en, item.zh))
                stats_line = format_stats(seg_s, item.asr_ms, item.tr_ms, item.tr_first_ms)
            display.end(item, stats_line)

    threads = [
        threading.Thread(target=capture, name="capture", daemon=True),
        threading.Thread(target=asr_worker, name="asr", daemon=True),
        threading.Thread(target=tr_worker, name="translate", daemon=True),
    ]

    def on_sigint(*_):
        stop.set()
        if ffmpeg.poll() is None:
            ffmpeg.terminate()

    signal.signal(signal.SIGINT, on_sigint)
    signal.signal(signal.SIGTERM, on_sigint)

    source = a.file or device_spec
    print(f"Listening on {source}  (Ctrl-C to stop){'' if translate else '  [no translation]'}")
    for t in threads:
        t.start()
    try:
        threads[2].join()  # translation worker exits after the capture pipeline drains
    finally:
        stop.set()
        if ffmpeg.poll() is None:
            ffmpeg.terminate()
        if httpd:
            httpd.shutdown()
        display.close()
        print(f"\nSaved: {display.txt_path}\n       {display.jsonl_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
