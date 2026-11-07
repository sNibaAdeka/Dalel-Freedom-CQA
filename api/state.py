"""Application state of the API server: storage, checklist versions, evaluator, runs, reviews, audio, settings.

Wiring only. Owners: storage/engine/checklist versions/runs/reviews -- a1 (cqa.db, cqa.engine, cqa.checklist_store,
cqa.runs, cqa.review); evaluator and LLM providers -- a2 (cqa.evaluator, cqa.llm); ingest/audio -- a3.

Evaluation settings (switch in the UI header): ``location`` cloud|local and ``mode`` fast|accurate. The provider
profile comes from config/models.json (a2): cloud -> "cloud" (or "claude"), local -> "local". Evaluations are stored
under a2's ``evaluator.mode_key(mode, profile)`` ("fast", "accurate:local", ...), so cloud and local answers never
overwrite each other.
"""
from __future__ import annotations

import inspect
import os
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from api.repo import Repo, plain
from cqa import audio as audio_mod
from cqa import ingest
from cqa.checklist_store import ChecklistStore

ROOT = Path(__file__).resolve().parents[1]
MODES = ("fast", "accurate")
LOCATIONS = ("cloud", "local")


def _llm():
    try:
        from cqa import llm
        return llm
    except Exception:  # a2 not merged yet / import error
        return None


def resolve_evaluator() -> Optional[Callable[..., list]]:
    """a2's evaluator, adapted to evaluate(conv, checklist, criterion_ids, mode=, profile=, live=) -> [evals]."""
    try:
        from cqa import evaluator
    except Exception:
        return None
    fn = None
    for name in ("evaluate_conversation", "evaluate", "evaluate_dialogue"):
        fn = getattr(evaluator, name, None)
        if callable(fn):
            break
    if fn is None:
        return None
    params = inspect.signature(fn).parameters
    ids_param = next((p for p in ("only_criteria", "criterion_ids", "criteria") if p in params), None)

    def call(conv, checklist, criterion_ids, mode="fast", profile=None, live=False):
        kwargs: dict[str, Any] = {}
        if ids_param:
            kwargs[ids_param] = list(criterion_ids)
        for key, value in (("mode", mode), ("profile", profile), ("no_cache", True if live else None), ("live", live)):
            if key in params:
                kwargs[key] = value
        out = fn(conv, checklist, **kwargs)
        if isinstance(out, dict):  # {"evals": [...], ...}
            out = out.get("evals") or out.get("criteria") or []
        return list(out or [])
    call.prompt_version = getattr(evaluator, "PROMPT_VERSION", None)  # type: ignore[attr-defined]
    call.is_a2 = True  # type: ignore[attr-defined]
    return call


class AppState:
    def __init__(self, db_path: str | Path | None = None, evaluate: Any = "auto", llm_call: Any = "auto",
                 resolver: Optional[audio_mod.AudioResolver] = None,
                 calibration: Optional[audio_mod.CalibrationStore] = None, web_dir: str | Path | None = None,
                 autoload: bool = True, calibrate_audio: bool = True, workers: Optional[int] = None):
        llm = _llm()
        if llm is not None:
            llm.load_dotenv()
        self.repo = Repo(db_path)
        self.db = self.repo.db
        self.checklists = ChecklistStore(self.db)
        self._evaluate = resolve_evaluator() if evaluate == "auto" else evaluate
        self._llm_call = llm_call
        self.resolver = resolver or audio_mod.AudioResolver()
        self.calibration = calibration or audio_mod.CalibrationStore()
        self.calibrate_audio = calibrate_audio
        self.web_dir = Path(web_dir or os.environ.get("CQA_WEB_DIR") or ROOT / "web")
        self.workers = int(workers or os.environ.get("CQA_RUN_WORKERS", 8))
        self.lock = threading.RLock()
        self.runs = self._make_runs()
        llm = _llm()
        if llm is not None and hasattr(llm, "add_listener"):
            llm.add_listener(self._on_llm_call)
        if autoload and not self.repo.conversations():
            self.autoload_dev()

    # --- settings --------------------------------------------------------------------------------------------------
    def settings(self) -> dict:
        mode = self.repo.get_setting("eval_mode", os.environ.get("CQA_MODE", "fast"))
        location = self.repo.get_setting("eval_location", "local" if os.environ.get("CQA_PROFILE") == "local" else "cloud")
        profile = self.repo.get_setting("eval_profile") or self._default_profile(location)
        return {"mode": mode if mode in MODES else "fast", "location": location, "profile": profile}

    def _default_profile(self, location: str) -> str:
        if location == "local":
            return "local"
        env = os.environ.get("CQA_PROFILE")
        return env if env and env != "local" else "cloud"

    def update_settings(self, mode: Optional[str] = None, location: Optional[str] = None,
                        profile: Optional[str] = None) -> dict:
        if mode is not None and mode not in MODES:
            raise ValueError(f"Режим {mode!r}: ожидается fast или accurate")
        if location is not None and location not in LOCATIONS:
            raise ValueError(f"Размещение {location!r}: ожидается cloud или local")
        profiles = self.profiles()
        if profile is not None and profiles and profile not in profiles:
            raise ValueError(f"Профиль {profile!r} не описан в config/models.json")
        if location is not None and profile is None:
            profile = self._default_profile(location)
        if profile is not None and location is None:
            location = "local" if profile == "local" else "cloud"
        self.repo.set_settings(eval_mode=mode, eval_location=location, eval_profile=profile)
        return self.settings()  # the profile is passed explicitly to the evaluator; no global env mutation

    def profiles(self) -> list[str]:
        llm = _llm()
        try:
            return [p for p in llm.config().get("profiles", {}) if not p.startswith("_")] if llm else []
        except Exception:
            return []

    def model_info(self, mode: Optional[str] = None, profile: Optional[str] = None) -> dict:
        s = self.settings()
        mode, profile = mode or s["mode"], profile or s["profile"]
        llm = _llm()
        if llm is None or not hasattr(llm, "preset"):
            return {"provider": "none", "model": "", "votes": None}
        try:
            p = llm.preset(mode, profile)
            provider, model = llm.parse_model(p["model"])
            arb = p.get("arbiter") or {}
            esc = p.get("escalate") or {}
            return {"provider": provider, "model": model, "votes": p.get("votes"),
                    "arbiter": arb.get("model") if isinstance(arb, dict) else None,
                    # cascade profile: stronger model that re-evaluates a dialogue when the first one marks a criterion for review
                    "escalate": llm.parse_model(esc["model"])[1] if isinstance(esc, dict) and esc.get("model") else None}
        except Exception as exc:
            return {"provider": "unknown", "model": "", "error": str(exc)[:200]}

    def eval_mode_key(self, mode: str, profile: str) -> str:
        """Storage key of the evaluation mode -- a2's evaluator.mode_key when available."""
        try:
            from cqa.evaluator import mode_key
            return mode_key(mode, profile)
        except Exception:
            return mode if profile in (None, "cloud") else f"{mode}:{profile}"

    # --- evaluator / LLM ---------------------------------------------------------------------------------------------
    @property
    def evaluator_ready(self) -> bool:
        return self._evaluate is not None

    def prompt_version(self) -> Optional[str]:
        return getattr(self._evaluate, "prompt_version", None)

    def prompt_version_for(self, criterion) -> Optional[str]:
        """Current evaluator prompt version of one criterion (a2: template + criterion block + active reading).
        An evaluation with another prompt_version is stale: the planner must not reuse it."""
        if self._evaluate is None or not getattr(self._evaluate, "is_a2", False):
            return None
        try:
            from cqa import prompts
            from cqa.evaluator import as_criterion
            return prompts.prompt_version(as_criterion(criterion))
        except Exception:
            return None

    def evaluate(self, conv, checklist, only_criteria=None, mode="fast", profile=None, no_cache=False) -> list:
        """Called by the run manager (a1) per conversation. ``mode`` is fast|accurate (a mode key such as
        "accurate:local" is split back); the evaluation is stored under ``eval_mode_key(mode, profile)``."""
        if self._evaluate is None:
            raise RuntimeError("Оценщик (cqa.evaluator, a2) ещё не подключён")
        base_mode, _, suffix = str(mode).partition(":")
        prof = profile or suffix or self.settings()["profile"]
        out = []
        criterion_ids = list(only_criteria) if only_criteria is not None else [c.id for c in checklist.criteria]
        for ev in self._evaluate(plain(conv), checklist, criterion_ids, mode=base_mode, profile=prof, live=no_cache):
            ev = plain(ev)
            ev["mode"] = self.eval_mode_key(base_mode, prof)  # same key the planner looks up
            ev.setdefault("profile", prof)
            out.append(ev)
        return out

    def llm_call(self) -> Optional[Callable[[str, str], tuple]]:
        """One JSON call with the fast model of the current profile (dossier, lint); None when unavailable/mock."""
        if self._llm_call is None:
            return None
        if callable(self._llm_call):
            return self._llm_call
        llm = _llm()
        if llm is None or not hasattr(llm, "complete_json"):
            return None
        s = self.settings()
        try:
            spec = llm.preset("fast", s["profile"])["model"]
        except Exception:
            return None
        if str(spec).startswith("mock:"):
            return None

        def call(system: str, user: str):
            return llm.complete_json(system, user, model=spec, temperature=0.0)
        return call

    def _on_llm_call(self, usage: dict) -> None:
        if usage.get('provider') == 'mock':
            return
        try:
            self.repo.log_llm_call({"provider": usage.get("provider", ""), "model": usage.get("model", ""),
                                    "purpose": usage.get("purpose", ""), "tokens_in": usage.get("tokens_in", 0),
                                    "tokens_out": usage.get("tokens_out", 0), "cost_usd": usage.get("cost_usd", 0.0),
                                    "cost_kzt": usage.get("cost_kzt", 0.0), "latency_ms": usage.get("latency_ms", 0),
                                    "cache_hit": bool(usage.get("cache_hit")), "ok": True})
        except Exception:
            pass
        on_call = getattr(self.runs, "on_llm_call", None)
        if callable(on_call):
            on_call(usage)

    # --- runs and reviews (a1) -----------------------------------------------------------------------------------------
    def _make_runs(self):
        from cqa.runs import RunManager
        return RunManager(self.db, self.checklists, evaluate=self.evaluate, workers=self.workers,
                          prompt_version=self.prompt_version_for)

    @property
    def review(self):
        from cqa.review import ReviewService
        profile = self.settings()['profile']
        modes = [self.eval_mode_key(mode, profile) for mode in ('fast', 'accurate')]
        return ReviewService(self.db, self.checklists, eval_modes=modes, prompt_version=self.prompt_version_for)

    # --- data --------------------------------------------------------------------------------------------------------
    def conversation(self, conv_id: str) -> Optional[dict]:
        """One stored dialogue as frontend JSON (used by scripts/control_run.py)."""
        return self.repo.conversation(conv_id)

    def ingest(self, convs: list[dict], source: str) -> dict:
        # a3: a dialogue whose id is taken by another group or by different content is stored as <id>@<group>
        # (meta.source_id keeps the organizers' id), so a control set never inherits stored evaluations.
        stored = {}
        for c in self.db.list_conversations():
            msgs = [m.model_dump() for m in c.messages]
            stored[c.id] = {"group_id": c.group_id,
                            "content_hash": (c.meta or {}).get("content_hash")
                            or ingest.content_hash(c.channel, c.language, msgs)}
        convs = ingest.resolve_id_collisions(convs, stored)
        new, updated = self.repo.upsert_conversations(convs)
        group = convs[0]["group_id"] if convs else None
        calls = [c for c in convs if c["channel"] == "call"]
        self.repo.log("ingest", group or "", "ingest.completed",
                      {"count": len(convs), "new": new, "updated": updated, "source": source, "calls": len(calls)})
        if self.calibrate_audio:
            for conv in calls:
                if not self.calibration.get(conv["id"]):
                    audio_mod.calibrate_in_background(conv, self.resolver, self.calibration)
        return {"count": len(convs), "group_id": group, "new": new, "updated": updated, "calls": len(calls),
                "source": source, "ids": [c["id"] for c in convs],
                "renamed": {c["meta"]["source_id"]: c["id"] for c in convs
                            if c["id"] != c["meta"].get("source_id", c["id"])}}

    def autoload_dev(self) -> Optional[dict]:
        path = ROOT / "data/raw/dev_records.json"
        if not path.exists():
            return None
        try:
            return self.ingest(ingest.ingest_file(path, group_id="DEV"), source="file")
        except Exception:
            return None
