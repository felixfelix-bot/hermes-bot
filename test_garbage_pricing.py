#!/usr/bin/env python3
"""Market-based garbage pricing tests — PLAN-garbage-output-detection.md.

Covers garbage_detector (detector suite, strikes, price multiplier, ledger) and
the flat_router.select_provider() garbage price hook:
  * each detector fires on its garbage class and passes on valid content
  * strikes escalate BASE^n and cap at inf; decay back to 1.0 after the window
  * alert-once-per-window via first_in_window
  * kill-switch file forces mult 1.0; cold start optimistic; fail-open
  * a penalized (provider, model) lane's effective cost is inflated in
    select_provider() so the market routes elsewhere
"""
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Path setup ──────────────────────────────────────────────────────────────
BOT = os.environ.get("HERMES_BOT_DIR", os.path.expanduser("~/.hermes/bot"))
MRE = os.environ.get("HERMES_MRE_DIR", os.path.expanduser("~/merchant-routing-engine"))
for p in [BOT, MRE, os.path.join(MRE, "src")]:
    if p not in sys.path:
        sys.path.insert(0, p)

import garbage_detector as gd  # noqa: E402

# Pin the LIVE zai_proxy import (same recipe as test_flat_router /
# test_garbage_circuit_breaker) so flat_router's lazy zai_proxy resolution
# gets the deployed source of truth.
import importlib.util as _ilu  # noqa: E402
_LIVE_ZAI_PROXY_PATH = os.path.join(BOT, "zai_proxy.py")
_zai_proxy_spec = _ilu.spec_from_file_location("zai_proxy", _LIVE_ZAI_PROXY_PATH)
_zai_proxy = _ilu.module_from_spec(_zai_proxy_spec)
sys.modules["zai_proxy"] = _zai_proxy
_zai_proxy_spec.loader.exec_module(_zai_proxy)

from flat_router import select_provider, _garbage_mult_or_one  # noqa: E402


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    """Reset strike/alert state + reroute the ledger to a tmp file for every
    test. Restores module config after each test."""
    gd.reset_state()
    gd._KILLSWITCH_TS["checked"] = 0.0
    gd._KILLSWITCH_TS["present"] = False
    monkeypatch.setattr(gd, "LEDGER_PATH", tmp_path / "garbage_ledger.jsonl")
    saved = {
        "PRICE_ENABLED": gd.PRICE_ENABLED,
        "LEDGER_ENABLED": gd.LEDGER_ENABLED,
        "PRICE_BASE_MULT": gd.PRICE_BASE_MULT,
        "PRICE_WINDOW": gd.PRICE_WINDOW,
        "PRICE_MAX_STRIKES": gd.PRICE_MAX_STRIKES,
        "NONLATIN_ENABLED": gd.NONLATIN_ENABLED,
        "BOT_DIR": gd.BOT_DIR,
    }
    yield gd
    for k, v in saved.items():
        setattr(gd, k, v)
    gd.reset_state()
    gd._KILLSWITCH_TS["checked"] = 0.0
    gd._KILLSWITCH_TS["present"] = False


# ── Content fixtures ────────────────────────────────────────────────────────

PROSE = (
    "The system completed the requested analysis successfully. All tests "
    "passed and the output files were written to the figures directory. "
    "Overall the pipeline looks healthy and ready for the next stage of "
    "the review process. Here is what happened during the run today."
)

CODE_BLOCK = (
    "```python\nimport os\n\nfor i in range(10):\n    print(i, flush=True)\n"
    "```\n\nThat code prints the numbers from zero to nine."
)

JSON_BODY = json.dumps({"plan": ["step one", "step two"], "status": "ok"})


def _json_resp(content, completion_tokens=50, prompt_tokens=25):
    return json.dumps({
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": prompt_tokens,
                  "completion_tokens": completion_tokens,
                  "total_tokens": prompt_tokens + completion_tokens},
    }).encode()


def _sse_resp(content_pieces, completion_tokens=50):
    parts = []
    for piece in content_pieces:
        parts.append("data: " + json.dumps(
            {"choices": [{"delta": {"content": piece}}]}) + "\n")
    parts.append("data: " + json.dumps(
        {"choices": [{"delta": {}}],
         "usage": {"prompt_tokens": 25,
                   "completion_tokens": completion_tokens,
                   "total_tokens": 25 + completion_tokens}}) + "\n")
    parts.append("data: [DONE]\n")
    return "".join(parts).encode()


# ── Detectors ───────────────────────────────────────────────────────────────

class TestDetectors:
    def test_empty(self):
        assert gd.looks_like_garbage("")["reason"] == "empty"
        assert gd.looks_like_garbage("   \n\t ")["reason"] == "empty"
        assert gd.looks_like_garbage(None)["reason"] == "empty"

    def test_repetition_compress(self):
        g = gd.looks_like_garbage("The quick brown fox jumped over the lazy dog. " * 200)
        assert g["reason"] == "repetition_compress"

    def test_repetition_compress_lines(self):
        # Identical lines always compress well, so exercise line-dominance
        # with a *large* low-entropy body: "yes" dominates the line count
        # while the embedded prose keeps the overall compression ratio
        # above the threshold (compress check must NOT fire first).
        lines = ["yes"] * 28 + [PROSE] + ["yes"]  # 30 lines, 29 x "yes"
        g = gd.looks_like_garbage("\n".join(lines))
        assert g["reason"] == "repetition_lines"

    def test_mojibake(self):
        g = gd.looks_like_garbage("hello world " * 10 + "\ufffd" * 20)
        assert g["reason"] == "mojibake"

    def test_control_chars(self):
        g = gd.looks_like_garbage("text goes here " * 40 + "\x07\x08\x00" * 30)
        assert g["reason"] == "control_chars"

    def test_bad_json_when_requested(self):
        req = {"model": "m", "response_format": {"type": "json_object"}}
        g = gd.looks_like_garbage("this is not json at all", req)
        assert g["reason"] == "bad_json"

    def test_valid_json_when_requested(self):
        req = {"model": "m", "response_format": {"type": "json_object"}}
        assert gd.looks_like_garbage(JSON_BODY, req) is None

    def test_fenced_json_accepted(self):
        req = {"model": "m", "response_format": {"type": "json_object"}}
        fenced = "```json\n" + JSON_BODY + "\n```"
        assert gd.looks_like_garbage(fenced, req) is None

    def test_no_json_contract_means_no_json_check(self):
        assert gd.looks_like_garbage("this is not json at all", {}) is None

    def test_gibberish_english(self):
        import random
        rng = random.Random(1234)
        consonants = "qwrtzpsdfghjklxcvbnm"
        words = ["".join(rng.choice(consonants) for _ in range(5))
                 for _ in range(180)]  # unique mash — high entropy, no words
        g = gd.looks_like_garbage(" ".join(words))
        assert g["reason"] == "gibberish_english"

    def test_valid_prose_passes(self):
        assert gd.looks_like_garbage(PROSE) is None

    def test_valid_code_passes(self):
        assert gd.looks_like_garbage(CODE_BLOCK) is None

    def test_nonlatin_spam_off_by_default(self):
        # 600 varied CJK chars: not repetitive, no English words
        cjk = "".join(chr(0x4E00 + (i * 7) % 4000) for i in range(600))
        assert len(cjk) > 200
        assert gd.looks_like_garbage(cjk) is None  # opt-in only

    def test_nonlatin_spam_when_enabled(self):
        gd.NONLATIN_ENABLED = True
        cjk = "".join(chr(0x4E00 + (i * 7) % 4000) for i in range(600))
        g = gd.looks_like_garbage(cjk)
        assert g and g["reason"] == "nonlatin_spam"


# ── Response parsing ─────────────────────────────────────────────────────────

class TestResponseParsing:
    def test_plain_json_clean(self):
        resp = _json_resp(PROSE)
        assert gd.inspect_response(resp) is None

    def test_plain_json_repetition(self):
        resp = _json_resp("The quick brown fox jumped over the lazy dog. " * 200)
        assert gd.inspect_response(resp)["reason"] == "repetition_compress"

    def test_oversized(self):
        resp = _json_resp(PROSE, completion_tokens=77_366)
        g = gd.inspect_response(resp)
        assert g["reason"] == "oversized"
        assert g["completion_tokens"] == 77_366

    def test_explicit_completion_tokens_hint(self):
        # usage absent from body — explicit hint still trips oversize
        resp = json.dumps({"choices": [{"message": {"content": PROSE}}]}).encode()
        g = gd.inspect_response(resp, completion_tokens=40_000)
        assert g["reason"] == "oversized"

    def test_sse_clean(self):
        resp = _sse_resp(["The system completed ", "the requested analysis ",
                          "successfully with all tests passing enough."])
        assert gd.inspect_response(resp) is None

    def test_sse_repetition(self):
        resp = _sse_resp(["all work and no play "] * 60)
        g = gd.inspect_response(resp)
        assert g["reason"] in ("repetition_compress", "repetition_lines")

    def test_error_body_not_judged(self):
        resp = json.dumps({"error": {"message": "overloaded"}}).encode()
        assert gd.inspect_response(resp) is None

    def test_no_choices_body_not_judged(self):
        resp = json.dumps({"id": "x", "usage": {"completion_tokens": 9}}).encode()
        assert gd.inspect_response(resp) is None

    def test_empty_body(self):
        assert gd.inspect_response(b"   \n")["reason"] == "empty_body"

    def test_garbage_types_never_raise(self):
        assert gd.inspect_response(None) is None
        assert gd.inspect_response(1337) is None
        assert gd.inspect_response(b"", None, 0) is not None or True

    def test_response_body_request_contract(self):
        req = json.dumps({"model": "m",
                          "response_format": {"type": "json_object"}}).encode()
        resp = _json_resp("not json, sorry")
        assert gd.inspect_response(resp, req)["reason"] == "bad_json"


# ── Strikes + market price multiplier ────────────────────────────────────────

class TestStrikesAndPrice:
    def _trip(self, provider="neuralwatt", model="glm-5.2", n=1):
        out = []
        for _ in range(n):
            out.append(gd.report_success_response(
                provider, model,
                _json_resp("The quick brown fox jumped over the lazy dog. " * 60)))
        return out

    def test_cold_start_optimistic(self):
        assert gd.garbage_price_mult("neuralwatt", "glm-5.2") == 1.0
        assert gd.strikes_in_window("neuralwatt", "glm-5.2") == 0

    def test_escalation_and_cap(self):
        gd.PRICE_BASE_MULT = 3.0
        gd.PRICE_MAX_STRIKES = 4
        self._trip(n=1)
        assert gd.garbage_price_mult("neuralwatt", "glm-5.2") == 3.0
        assert gd.garbage_price_mult("neuralwatt", "GLM-5.2") == 3.0  # case-insensitive
        self._trip(n=1)
        assert gd.garbage_price_mult("neuralwatt", "glm-5.2") == 9.0
        self._trip(n=1)
        assert gd.garbage_price_mult("neuralwatt", "glm-5.2") == 27.0
        self._trip(n=1)
        assert gd.garbage_price_mult("neuralwatt", "glm-5.2") == float("inf")

    def test_sibling_lanes_unaffected(self):
        gd.PRICE_BASE_MULT = 3.0
        self._trip(provider="neuralwatt", model="glm-5.2")
        assert gd.garbage_price_mult("neuralwatt", "kimi-k3") == 1.0
        assert gd.garbage_price_mult("deepinfra", "glm-5.2") == 1.0

    def test_window_decay(self):
        gd.PRICE_WINDOW = 0.2
        gd.PRICE_BASE_MULT = 3.0
        self._trip(n=2)
        assert gd.garbage_price_mult("neuralwatt", "glm-5.2") == 9.0
        time.sleep(0.25)
        assert gd.garbage_price_mult("neuralwatt", "glm-5.2") == 1.0

    def test_pass_through_clean_response_records_nothing(self):
        assert gd.report_success_response(
            "neuralwatt", "glm-5.2", _json_resp(PROSE)) is None
        assert gd.strikes_in_window("neuralwatt", "glm-5.2") == 0

    def test_killswitch_forces_one(self):
        gd.PRICE_BASE_MULT = 3.0
        self._trip(n=3)
        with patch.object(gd, "BOT_DIR",
                          Path(_mk_killswitch_dir_with_flag())):
            gd._KILLSWITCH_TS["checked"] = 0.0  # bust the 30s cache
            assert gd.garbage_price_mult("neuralwatt", "glm-5.2") == 1.0

    def test_disabled_price_never_mults(self):
        gd.PRICE_ENABLED = False
        self._trip(n=3)
        assert gd.garbage_price_mult("neuralwatt", "glm-5.2") == 1.0

    def test_fail_open_on_bad_input(self):
        for bad in (None, 3, object(), b"\xff\xfe\x00", {"a": 1}):
            assert gd.report_success_response(
                "p", "m", bad) is None or True  # never raises
        assert gd.garbage_price_mult(None, None) in (1.0,)
        assert _garbage_mult_or_one("neuralwatt", "glm-5.2") == 1.0


def _mk_killswitch_dir_with_flag() -> str:
    import tempfile
    d = tempfile.mkdtemp(prefix="gd-ks-")
    open(os.path.join(d, ".disable_garbage_pricing"), "w").close()
    return d


# ── Alert-once + ledger ──────────────────────────────────────────────────────

class TestAlertOnceAndLedger:
    def _trip(self):
        return gd.report_success_response(
            "neuralwatt", "glm-5.2",
            _json_resp("The quick brown fox jumped over the lazy dog. " * 60))

    def test_first_in_window_once(self):
        gd.PRICE_WINDOW = 3600
        assert self._trip()["first_in_window"] is True
        assert self._trip()["first_in_window"] is False
        assert self._trip()["first_in_window"] is False

    def test_rearm_after_window(self):
        gd.PRICE_WINDOW = 0.2
        assert self._trip()["first_in_window"] is True
        time.sleep(0.25)
        # window expired AND alert window expired → new trip re-alerts
        assert self._trip()["first_in_window"] is True

    def test_ledger_entries_written(self):
        gd.PRICE_BASE_MULT = 3.0
        self._trip()
        self._trip()
        events = gd.ledger_events()
        assert len(events) == 2
        assert events[0]["reason"] == "repetition_compress"
        assert events[0]["provider"] == "neuralwatt"
        assert events[1]["strikes_in_window"] == 2
        assert events[1]["price_mult"] == 9.0
        assert "snippet" in events[0]

    def test_ledger_rotation(self):
        gd.LEDGER_MAX_BYTES = 512
        for _ in range(4):
            self._trip()
        rotated = Path(gd.LEDGER_PATH).with_name(
            Path(gd.LEDGER_PATH).name + ".1")
        assert rotated.exists()
        assert gd.ledger_events(limit=100)  # fresh file still readable


# ── Router integration (flat_router market mechanism) ───────────────────────

class TestRouterIntegration:
    MODEL = "glm-5.2"

    def _cands(self):
        return select_provider(model=self.MODEL)

    def _lane(self):
        """Pick the cheapest eligible lane from LIVE state (providers'
        health varies day-to-day; the test must not hardcode a lane)."""
        for c in self._cands():
            if c.name != "fallback" and c.effective_cost < float("inf"):
                return c.name, c.model
        pytest.skip("no viable lane for glm-5.2 in current live state")

    def _strike(self, lane, model, n=2):
        for _ in range(n):
            gd.report_success_response(
                lane, model,
                _json_resp("The quick brown fox jumped over the lazy dog. " * 60))

    def test_select_provider_still_works_clean(self):
        cands = self._cands()
        assert len(cands) >= 1
        names = [c.name for c in cands]
        assert "fallback" not in names or len(names) == 1  # live lanes exist

    def test_penalized_lane_cost_inflated(self):
        lane, dispatch_model = self._lane()
        before = {c.name: c.effective_cost for c in self._cands()}
        assert before[lane] < float("inf")

        gd.PRICE_BASE_MULT = 3.0
        self._strike(lane, dispatch_model, n=2)  # mult 9x

        after_cands = self._cands()
        after = {c.name: c.effective_cost for c in after_cands}
        # The penalized lane is inflated ~9x (dynamic Kalman price may drift
        # slightly between calls; require clearly inflated cost) ...
        assert after[lane] >= before[lane] * 3.0
        # ... its reason names the penalty ...
        lane_reason = [c.reason for c in after_cands if c.name == lane][0]
        assert "garbage×" in lane_reason
        # ... and clean lanes are untouched.
        for name in before:
            if name != lane and before[name] < float("inf"):
                assert abs(after[name] - before[name]) < max(0.05, before[name] * 0.5)

    def test_penalized_lane_sinks_but_stays_eligible(self):
        # Strike the CURRENT cheapest lane to the cap (inf) — it must sink
        # in the ordering yet remain an eligible candidate: the penalty is
        # SOFT (market-based), never a removal.
        lane, dispatch_model = self._lane()
        self._strike(lane, dispatch_model, n=4)  # cap -> inf

        after = [c.name for c in self._cands()]
        assert lane in after                                # still eligible
        assert after.index(lane) > 0                        # no longer first
        assert after[-1] == lane                            # inf sorts to end

    def test_garbage_mult_helper_fail_open(self):
        gd.reset_state()
        assert _garbage_mult_or_one("neuralwatt", "glm-5.2") == 1.0


# ── G3: Keying-consistency test ──────────────────────────────────────────────
# Flat router's price lookup uses _resolve_model_for_provider(name, model) for
# the garbage multiplier key. But _garbage_check records strikes under the raw
# model string. If those diverge (e.g. "deepseek/deepseek-v4-flash" on the
# strike side vs "deepseek-v4-flash" on the price side), the price bump
# computes a mult for a key NO strike exists on = silent no-op.
# The fix: _garbage_check must resolve the model via the same helper.

class TestKeyingConsistency:
    """G3: A strike recorded by _garbage_check must land on the SAME
    (provider, model) key that flat_router's price lookup uses."""

    def test_strike_key_matches_price_lookup_key(self):
        from flat_router import _resolve_model_for_provider, PROVIDER_MODELS
        from zai_proxy import _garbage_check
        gd.reset_state()

        # Garbage that triggers repetition detection
        test_body = b'{"choices":[{"message":{"content":"' + b'test ' * 200 + b'"}}],"usage":{"completion_tokens":500}}'

        failures = []
        for provider, models in PROVIDER_MODELS.items():
            for m in sorted(models):
                resolved = _resolve_model_for_provider(provider, m)
                key_model = resolved if resolved else m
                # Record a strike via _garbage_check — it should use
                # the SAME resolved key as the price lookup.
                _garbage_check(provider, m, test_body)
                if gd.strikes_in_window(provider, key_model) < 1:
                    raw_hits = gd.strikes_in_window(provider, m)
                    failures.append(
                        f"  {provider}/{m}: strike on raw='{m}' (hits={raw_hits}) "
                        f"but price-lookup key='{key_model}' has 0 hits")
                gd.reset_state()

        assert not failures, (
            "Keying divergence — price-lookup key differs from strike-recording key "
            "for these (provider, model):\n" + "\n".join(failures[:5]))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
