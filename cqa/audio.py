"""Call audio: a fresh mp3 link from the organizers' HTML player and playback offsets per message.

``audio_url`` of a call is a Power Automate link that returns an HTML page with
``<audio><source src="https://...dynamics.com/api/filedownload/...?...&se=<expiry>&..."></audio>``. The mp3 link is a
signed SAS URL valid ~1 hour (``se=``); the server supports Range requests, so the browser can seek on it directly.
We fetch the page on demand, cache the link until shortly before ``se`` and never persist it.

Offsets. ``ingest`` gives ``rec_sec`` on the event clock (time - recording start - pauses). Measured on the 5 DEV calls
with ffmpeg silencedetect: turn timestamps mark the END of an utterance, and the mp3 runs ahead of the event clock by a
per-call lead (4.0-4.9 s when the recording starts with the call, ~0.7 s when it starts mid-call, p4-ru-002).
Playback offset = estimated utterance start (end - len(text)/12 cps, not before the previous turn) + lead.
The lead is fitted per call by VAD (``calibrate``: download, detect speech->silence boundaries, maximize matches) in the
background and stored in ``.cache/audio/calibration.json``; until then a heuristic lead is used.
"""
from __future__ import annotations

import html as html_lib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
CACHE_DIR = Path(os.environ.get("CQA_AUDIO_CACHE", ROOT / ".cache/audio"))
LEAD_CALL_START = 4.3      # median of the fitted leads of the 4 DEV calls whose recording starts with the call
LEAD_MID_CALL = 0.7        # fitted lead of p4-ru-002 (recording started 30 s into the call)
CHARS_PER_SEC = 12.0       # speech rate used to estimate the start of an utterance from its end
REFRESH_MARGIN = 120       # seconds before ``se`` when a cached link is refreshed
DEFAULT_TTL = 50 * 60      # when the link has no ``se``
PAUSE_SEC = 3.0            # a "long" pause for the informational speech block (cqa.speech)

_SRC_RE = re.compile(r"<(?:audio|source)\b[^>]*?\ssrc\s*=\s*([\"'])(.*?)\1", re.I | re.S)


# --- link ------------------------------------------------------------------------------------------------------

def extract_src(page: str) -> str | None:
    """First http(s) ``src`` of an <audio> or <source> element in the player page."""
    for match in _SRC_RE.finditer(page or ""):
        src = html_lib.unescape(match.group(2)).strip()
        if src.lower().startswith(("https://", "http://")):
            return src
    return None


def expires_at(src: str) -> datetime | None:
    values = parse_qs(urlsplit(src).query).get("se")
    if not values:
        return None
    text = values[0].strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _default_fetch(url: str) -> str:
    import httpx

    resp = httpx.get(url, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


class AudioResolver:
    """Resolves and caches the signed mp3 link of each call (in memory only)."""

    def __init__(self, fetch_html=None, now=None, margin: int = REFRESH_MARGIN):
        self.fetch_html = fetch_html or _default_fetch
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.margin = margin
        self._cache: dict[str, tuple[str, datetime]] = {}
        self._lock = threading.Lock()

    def resolve(self, conv_id: str, player_url: str | None, force: bool = False) -> dict:
        if not player_url:
            return {"src": None, "expires_at": None, "error": "У диалога нет ссылки на запись."}
        with self._lock:
            hit = self._cache.get(conv_id)
        if hit and not force and (hit[1] - self.now()).total_seconds() > self.margin:
            return {"src": hit[0], "expires_at": _iso(hit[1]), "error": None}
        try:
            src = extract_src(self.fetch_html(player_url))
        except Exception as exc:  # network, HTTP status, timeout
            return {"src": None, "expires_at": None, "error": f"Плеер организаторов недоступен: {type(exc).__name__}"}
        if not src:
            return {"src": None, "expires_at": None, "error": "В ответе плеера нет ссылки на запись."}
        exp = expires_at(src) or (self.now() + timedelta(seconds=DEFAULT_TTL))
        with self._lock:
            self._cache[conv_id] = (src, exp)
        return {"src": src, "expires_at": _iso(exp), "error": None}


# --- offsets ---------------------------------------------------------------------------------------------------

def utterance_starts(conv: dict) -> dict[str, float]:
    """Estimated start of each speech turn on the event clock (rec_sec marks its end)."""
    out: dict[str, float] = {}
    prev = None
    for m in conv.get("messages", []):
        r = m.get("rec_sec")
        if r is None:
            continue
        if m["speaker"] in ("client", "operator", "bot"):
            dur = min(12.0, max(0.8, len(m.get("text") or "") / CHARS_PER_SEC))
            start = r - dur
            if prev is not None:
                start = max(start, min(prev, r))
            out[m["id"]] = max(0.0, start)
        else:
            out[m["id"]] = r
        prev = r
    return out


def default_lead(conv: dict) -> float:
    start = (conv.get("meta") or {}).get("recording_start_sec") or 0
    return LEAD_CALL_START if start < 5 else LEAD_MID_CALL


def playback_offsets(conv: dict, lead: float) -> dict[str, float]:
    return {mid: round(max(0.0, s + lead), 1) for mid, s in utterance_starts(conv).items()}


def with_playback_offsets(conv: dict, lead: float | None = None) -> dict:
    """Copy of the conversation whose messages carry playable ``rec_sec`` (event clock kept in ``rec_sec_event``)."""
    if conv.get("channel") != "call":
        return conv
    offsets = playback_offsets(conv, default_lead(conv) if lead is None else lead)
    out = dict(conv)
    out["messages"] = []
    for m in conv["messages"]:
        mm = dict(m)
        mm["rec_sec_event"] = m.get("rec_sec")
        mm["rec_sec"] = offsets.get(m["id"])
        out["messages"].append(mm)
    return out


# --- VAD calibration ---------------------------------------------------------------------------------------------

def fit_lead(ends: list[float], silence_starts: list[float], tol: float = 1.0,
             lo: float = -2.0, hi: float = 10.0, step: float = 0.1) -> tuple[float, float]:
    """Shift that puts the most utterance ends next to a speech->silence boundary; centre of the best plateau."""
    if not ends or not silence_starts:
        return 0.0, 0.0
    marks = sorted(silence_starts)
    n = int(round((hi - lo) / step))
    scores = []
    for i in range(n + 1):
        c = lo + i * step
        scores.append((sum(any(abs(t + c - m) <= tol for m in marks) for t in ends), round(c, 2)))
    best = max(s for s, _ in scores)
    plateau = [c for s, c in scores if s == best]
    return round(median(plateau), 2), round(best / len(ends), 3)


def silence_profile(mp3: Path, noise: str = "-35dB", min_dur: float = 0.5) -> tuple[list[tuple[float, float]], float | None]:
    """ffmpeg silencedetect: ([(start, end|None)], duration of the file in seconds or None)."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is not installed")
    proc = subprocess.run([ffmpeg, "-hide_banner", "-nostats", "-i", str(mp3), "-af",
                           f"silencedetect=noise={noise}:d={min_dur}", "-f", "null", "-"],
                          capture_output=True, text=True, timeout=120)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", proc.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", proc.stderr)]
    ends += [None] * (len(starts) - len(ends))  # trailing silence runs to the end of the file
    dur = re.search(r"Duration: (\d+):(\d+):([\d.]+)", proc.stderr)
    duration = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)) if dur else None
    return list(zip(starts, ends)), duration


def detect_silences(mp3: Path, noise: str = "-35dB", min_dur: float = 0.5) -> list[tuple[float, float]]:
    return silence_profile(mp3, noise, min_dur)[0]


def silence_stats(silences: list, duration: float | None, threshold: float = PAUSE_SEC) -> dict | None:
    """Informational summary (``cqa.speech``) of silencedetect intervals [(start, end|None)] of a recording."""
    if not duration or duration <= 0:
        return None
    lengths = []
    for start, end in silences:
        end = duration if end is None else min(float(end), duration)
        if end > start:
            lengths.append(end - float(start))
    total = sum(lengths)
    return {"duration_sec": round(duration, 1), "silence_sec": round(total, 1),
            "silence_share": round(min(1.0, total / duration), 3),
            "pauses_over_3s": sum(1 for x in lengths if x > threshold),
            "longest_silence_sec": round(max(lengths), 1) if lengths else 0.0}


class CalibrationStore:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or CACHE_DIR / "calibration.json")
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def get(self, conv_id: str) -> dict | None:
        return self._read().get(conv_id)

    def put(self, conv_id: str, value: dict) -> None:
        with self._lock:
            data = self._read()
            data[conv_id] = value
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.path)


def calibrate(conv: dict, resolver: AudioResolver, store: CalibrationStore, download=None) -> dict | None:
    """Download the mp3 to a temp dir, fit the lead by VAD, store it, delete the audio. None on failure."""
    link = resolver.resolve(conv["id"], conv.get("audio_url"))
    if not link["src"] or not shutil.which("ffmpeg"):
        return None
    tmp = Path(tempfile.mkdtemp(prefix="cqa-audio-"))
    try:
        mp3 = tmp / "call.mp3"
        if download:
            mp3.write_bytes(download(link["src"]))
        else:
            import httpx

            with httpx.stream("GET", link["src"], timeout=120, follow_redirects=True) as resp:
                resp.raise_for_status()
                with mp3.open("wb") as fh:
                    for chunk in resp.iter_bytes():
                        fh.write(chunk)
        silences, duration = silence_profile(mp3)
        ends = [m["rec_sec"] for m in conv["messages"]
                if m.get("rec_sec") is not None and m["speaker"] in ("client", "operator", "bot")]
        lead, hit = fit_lead(ends, [s for s, _ in silences])
        if hit < 0.5:  # no reliable alignment: keep the heuristic
            value = {"lead_sec": default_lead(conv), "hit_rate": hit, "method": "heuristic",
                     "note": "VAD alignment below 50%"}
        else:
            value = {"lead_sec": lead, "hit_rate": hit, "method": "vad"}
        value.update({"n_turns": len(ends), "computed_at": _iso(datetime.now(timezone.utc)),
                      "silence": silence_stats(silences, duration)})
        store.put(conv["id"], value)
        return value
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_running: set[str] = set()
_running_lock = threading.Lock()


def calibrate_in_background(conv: dict, resolver: AudioResolver, store: CalibrationStore) -> bool:
    with _running_lock:
        if conv["id"] in _running:
            return False
        _running.add(conv["id"])

    def job():
        try:
            calibrate(conv, resolver, store)
        finally:
            with _running_lock:
                _running.discard(conv["id"])

    threading.Thread(target=job, name=f"cqa-audio-{conv['id']}", daemon=True).start()
    return True


# --- response --------------------------------------------------------------------------------------------------

def lead_for(conv: dict, store: CalibrationStore | None) -> tuple[float, str, dict | None]:
    cal = store.get(conv["id"]) if store else None
    if cal and isinstance(cal.get("lead_sec"), (int, float)):
        return float(cal["lead_sec"]), cal.get("method", "vad"), cal
    return default_lead(conv), "heuristic", None


def payload(conv: dict, resolver: AudioResolver, store: CalibrationStore | None = None, calibrate: bool = True) -> dict:
    """``GET /api/audio/{id}``: {src, expires_at, offsets, gaps, ...}; ``src`` is null when there is no recording."""
    meta = conv.get("meta") or {}
    if conv.get("channel") != "call":
        return {"conv_id": conv["id"], "src": None, "expires_at": None, "offsets": {}, "gaps": [], "gaps_rec": [],
                "error": "Это чат: записи нет."}
    link = resolver.resolve(conv["id"], conv.get("audio_url"))
    lead, method, cal = lead_for(conv, store)
    if calibrate and store is not None and (cal is None or "silence" not in cal) and link["src"]:
        calibrate_in_background(conv, resolver, store)
    return {"conv_id": conv["id"], "src": link["src"], "expires_at": link["expires_at"],
            "offsets": playback_offsets(conv, lead), "gaps": meta.get("recording_gaps", []),
            "gaps_rec": meta.get("recording_gaps_rec", []), "recording_start_sec": meta.get("recording_start_sec"),
            "duration_est": (meta.get("recording_duration_sec") or 0) + lead if meta.get("recording_duration_sec") else None,
            "lead_sec": lead, "offset_method": method, "offsets_are_estimates": True,
            "calibration": cal, "error": link["error"]}
