from io import BytesIO
from urllib.error import HTTPError
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from rubric.mtqc import JUDGES
from rubric.mtqc.cli import run
from conftest import FakeAPI, business_result


@pytest.mark.parametrize("bad_result", ["factor_two", "contradiction", "missing_failed_factors", "truncated", "timeout"])
def test_judge_failures_retry_without_quality_scores(settings, bad_result):
    config = settings()
    def callback(name, payload, number):
        if name == JUDGES[0]:
            value = business_result(name, payload)
            if bad_result == "factor_two":
                value["score"] = 2
            elif bad_result == "contradiction":
                value["failed_factors"] = business_result(name, payload, 1)["failed_factors"]
            elif bad_result == "missing_failed_factors":
                del value["failed_factors"]
            elif bad_result == "truncated":
                return json.dumps({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]})
            elif bad_result == "timeout":
                return TimeoutError("timed out")
            return value
        return business_result(name, payload)
    api = FakeAPI(config, callback)
    result = run(config, api)[0]
    assert result["final_scores"][JUDGES[0]] is None
    assert result["final_scores"][JUDGES[1]] == 3
    assert result["stop_reason"] == "JUDGE_ERROR" and result["overall_decision"] == "REVIEW"
    assert result["repair_attempt_count"] == 0
    assert api.counts == {JUDGES[0]: 3, **dict.fromkeys(JUDGES[1:], 1)}
    run(config, api)
    assert len(api.calls) == 6  # exhausted attempts are never reset by resume


def test_transient_failure_recovers(settings):
    config = settings()
    def callback(name, payload, number):
        if name == JUDGES[0] and number < 3:
            return HTTPError("https://example.com", 429, "rate limited", {}, BytesIO(b"retry"))
        return business_result(name, payload)
    api = FakeAPI(config, callback)
    result = run(config, api)[0]
    assert result["stop_reason"] == "PASS_INITIAL"
    assert result["revisions"][0]["evaluation"]["evaluation_status"] == "COMPLETED"
    assert api.counts[JUDGES[0]] == 3


def test_authentication_failure_is_permanent_and_redacted(settings):
    config = settings(llm={"api_key": "secret-test-key"})
    def callback(name, payload, number):
        if name == JUDGES[0]:
            return HTTPError("https://example.com", 401, "auth", {}, BytesIO(b"secret-test-key"))
        return business_result(name, payload)
    api = FakeAPI(config, callback)
    with pytest.raises(ValueError, match="HTTP 401"):
        run(config, api)
    from pathlib import Path
    result = json.loads(Path(config["output"]["results_jsonl"]).read_text())
    assert api.counts[JUDGES[0]] == 1
    assert result["stop_reason"] == "JUDGE_ERROR"
    assert "secret-test-key" not in (Path(config["output"]["records_dir"]) / "attempts.jsonl").read_text()
    assert "secret-test-key" not in (Path(config["output"]["records_dir"]) / "manifest.json").read_text()
    with pytest.raises(ValueError, match="Permanent API failure"):
        run(config, api)
    assert api.counts[JUDGES[0]] == 1


def test_fail_fast_only_stops_on_quality_one(settings):
    config = settings(repair={"enabled": False}, evaluation={"fail_fast": True})
    api = FakeAPI(config, lambda name, payload, number: business_result(name, payload, 1))
    result = run(config, api)[0]
    assert len(api.calls) == 1
    assert result["stop_reason"] == "EARLY_STOP"
    assert result["overall_decision"] == "REPAIR"
    assert result["final_scores"] == {JUDGES[0]: 1, **dict.fromkeys(JUDGES[1:])}
    assert result["revisions"][0]["evaluation"]["evaluation_status"] == "PARTIAL"


def test_fail_fast_does_not_stop_on_technical_failure(settings):
    config = settings(repair={"enabled": False}, evaluation={"fail_fast": True})
    api = FakeAPI(config, lambda name, payload, number: TimeoutError("network") if name == JUDGES[0] else business_result(name, payload))
    result = run(config, api)[0]
    assert all(api.counts[name] >= 1 for name in JUDGES)
    assert result["stop_reason"] == "JUDGE_ERROR"


@pytest.mark.parametrize("api_path", ["/v1", "/v1/chat/completions"])
def test_concurrency_limit_and_actual_http_wire(settings, monkeypatch, api_path):
    config = settings(llm={"concurrency": 2, "api_key": "http-test-key"})
    fake = FakeAPI(config)
    lock = threading.Lock()
    active, peak = 0, 0
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal active, peak
            assert self.path == "/v1/chat/completions"
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.headers["Authorization"] == "Bearer http-test-key"
            assert body["response_format"]["type"] == "json_schema"
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            raw = fake("", "", body, 1).encode()
            with lock:
                active -= 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = settings(llm={"concurrency": 2, "api_key": "http-test-key",
                           "api_url": f"http://127.0.0.1:{server.server_port}{api_path}"})
    monkeypatch.setenv("OPENAI_API_KEY", "ignored-environment-key")
    try:
        assert run(config)[0]["eligible_for_ingestion"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert 1 < peak <= 2 and len(fake.calls) == 4


@pytest.mark.parametrize("raw", ['{"choices":[3]}', '{"choices":[{"message":null}]}', '{"choices":[]}', '{"choices":[{"finish_reason":"stop","message":{"content":null}}]}'])
def test_malformed_api_envelopes_retry(settings, raw):
    config = settings()
    api = FakeAPI(config, lambda name, payload, number: raw if name == JUDGES[0] else business_result(name, payload))
    result = run(config, api)[0]
    assert result["stop_reason"] == "JUDGE_ERROR" and api.counts[JUDGES[0]] == 3


def test_json_object_mode_still_validates_locally(settings):
    config = settings(llm={"response_format": "json_object"})
    api = FakeAPI(config)
    assert run(config, api)[0]["eligible_for_ingestion"]
    assert all(body["response_format"] == {"type": "json_object"} for _, _, body in api.calls)


def test_permanent_repair_api_error_aborts_remaining_batch(settings, case):
    from copy import deepcopy
    from pathlib import Path
    other = deepcopy(case)
    other["case_id"] = "case-2"
    config = settings([case, other])
    def callback(name, payload, number):
        if name == "dialogue_repair":
            return HTTPError("https://example.com", 400, "unsupported", {}, BytesIO(b"unsupported model"))
        return business_result(name, payload, 1 if name == JUDGES[0] else 3)
    api = FakeAPI(config, callback)
    with pytest.raises(ValueError, match="HTTP 400"):
        run(config, api)
    assert len(api.calls) == 5
    results = [json.loads(line) for line in Path(config["output"]["results_jsonl"]).read_text().splitlines()]
    assert results[0]["stop_reason"] == "REPAIR_ERROR"
    assert results[1]["stop_reason"] == "INTERRUPTED"
