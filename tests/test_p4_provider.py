import http.server
import json
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

import llm_provider
import model_tasks
import recordings
import storage
import workflow


class OpenCodeProviderTests(unittest.TestCase):
    def test_generate_uses_fixed_model_and_only_minimal_task_fields(self):
        client = llm_provider.OpenCodeTextClient()
        task = {"kind": "conspect", "recording_id": "synthetic", "source_revision": 1,
                "review_revision": 1, "goal": "кратко",
                "words": [{"word_id": "w1", "text": "Синтетика", "uncertain": False}]}
        with mock.patch.object(client, "_request", return_value={"data": {"text": "{\"source_revision\":1}"}}) as call:
            result, raw = client.generate(task)
        self.assertEqual((result, raw), ({"source_revision": 1}, "{\"source_revision\":1}"))
        payload = call.call_args.args[1]
        self.assertEqual(payload["model"], {"providerID": "openai", "id": "gpt-6-luna"})
        self.assertNotIn("recording_id", payload["prompt"])
        self.assertIn("Синтетика", payload["prompt"])
        self.assertEqual(call.call_args.args[0], "/api/experimental/generate/text")

    def test_malformed_model_json_is_returned_for_one_repair(self):
        client = llm_provider.OpenCodeTextClient()
        task = {"kind": "conspect", "recording_id": "synthetic", "source_revision": 1,
                "review_revision": 1, "goal": "",
                "words": [{"word_id": "w1", "text": "слово", "uncertain": False}]}
        with mock.patch.object(client, "_request", return_value={"data": {"text": "not json"}}):
            result, raw = client.generate(task)
        self.assertIsNone(result)
        self.assertEqual(raw, "not json")

    def test_local_api_request_bypasses_configured_proxy(self):
        target_hits, proxy_hits = [], []

        class Target(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                target_hits.append((self.path, self.headers.get("Authorization"),
                                    self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

            def log_message(self, *args):
                pass

        class Proxy(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                proxy_hits.append((self.path, self.headers.get("Authorization")))
                self.send_response(502)
                self.end_headers()

            def log_message(self, *args):
                pass

        target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Target)
        proxy = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        for server in (target, proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = llm_provider.OpenCodeTextClient()
            client.base_url = f"http://127.0.0.1:{target.server_port}"
            client.auth = "Basic synthetic-local-secret"
            with mock.patch.dict(os.environ, {
                "HTTP_PROXY": f"http://127.0.0.1:{proxy.server_port}",
                "http_proxy": f"http://127.0.0.1:{proxy.server_port}",
                "NO_PROXY": "", "no_proxy": "", "ALL_PROXY": "", "all_proxy": "",
            }):
                result = client._request("/api/info", {"prompt": "синтетический текст"}, timeout=3)
            self.assertEqual(result, {"ok": True})
            self.assertEqual(len(target_hits), 1)
            self.assertEqual(target_hits[0][1], "Basic synthetic-local-secret")
            self.assertIn("синтетический текст".encode("utf-8"), target_hits[0][2])
            self.assertEqual(proxy_hits, [])
        finally:
            target.shutdown()
            proxy.shutdown()
            target.server_close()
            proxy.server_close()


class AutonomousWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="p4-workflow-"))
        self.source = self.root / "synthetic.wav"
        self.source.write_bytes(b"synthetic")
        self.store = storage.initialize(self.root / "state.sqlite3", initial_display_name="Тест")

    def tearDown(self):
        self.store.close()

    def prepare_request(self, count=1, allow_cloud=True):
        request = workflow.create(self.store, self.source, {"require_conspect": True,
                                                            "allow_cloud_model": allow_cloud})
        workflow.claim_start(self.store, request)
        layout = recordings.document_layout(self.source, "")
        layout["root"].mkdir()
        root_id, paths = recordings.prepare_new_layout(self.store, layout["root"], layout["paths"])
        recording_id = str(uuid.uuid4())
        data = {"file": str(self.source), "duration": "00:00:01", "speakers": ["Спикер 1"],
                "quality": {}, "review": [], "review_done": False,
                "segments": [{"start": 0, "end": count, "speaker": "Спикер 1",
                              "words": [{"w": f"w{i}", "s": i, "e": i + 1} for i in range(count)]}]}
        recordings.commit_worker_result(
            self.store, data, self.source, root_id, recordings.operation_context(self.store, "model"),
            recording_id=recording_id, operation_id=str(uuid.uuid4()),
            generated_at="2026-01-01T00:00:00Z", document_paths=paths)
        recordings.materialize_recording(self.store, recording_id)
        with self.store._transaction():
            self.store.db.execute("UPDATE workflow_requests SET recording_id=?, state='processed' WHERE request_id=?",
                                  (recording_id, request))
        return request

    def test_one_invalid_response_gets_one_repair_then_completes(self):
        request = self.prepare_request()
        valid = {"source_revision": 1, "sections": [{"text": "Синтетический итог.",
                                                      "evidence": [workflow.prepare_model_task(
                                                          self.store, request)["words"][0]["word_id"]]}]}

        class FakeClient:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def generate(self, task, *, repair_error=None, invalid_output=None):
                if repair_error is None:
                    return {"source_revision": 1, "sections": [{"text": "Итог.", "evidence": ["unknown"]}]}, "bad"
                self.assert_repair = (repair_error, invalid_output)
                return valid, '{"source_revision":1}'

        with mock.patch("llm_provider.OpenCodeTextClient", FakeClient):
            result = workflow.execute_model(self.store, request)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(workflow.get(self.store, request)["model_attempts"], 1)
        self.assertEqual(len(self.store.list_conspects(workflow.get(self.store, request)["recording_id"])), 1)

    def test_provider_failure_is_visible_and_keeps_transcript(self):
        request = self.prepare_request()

        class FailingClient:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def generate(self, *args, **kwargs):
                raise llm_provider.ProviderError("rate_limit", "OpenAI ограничил частоту запросов")

        with mock.patch("llm_provider.OpenCodeTextClient", FailingClient):
            with self.assertRaises(llm_provider.ProviderError):
                workflow.execute_model(self.store, request)
        saved = workflow.get(self.store, request)
        self.assertEqual(saved["state"], "failed")
        self.assertIsNotNone(self.store.get_current(saved["recording_id"]))
        self.assertEqual(self.store.list_conspects(saved["recording_id"]), [])
        failure = self.store.db.execute("SELECT result_json FROM workflow_steps "
                                        "WHERE state='failed' AND run_id=?", (saved["model_run_id"],)).fetchone()
        self.assertIn("rate_limit", failure["result_json"])

    def test_retry_resumes_only_missing_blocks(self):
        request = self.prepare_request(count=501)
        task = workflow.prepare_model_task(self.store, request)
        first = workflow.model_part(self.store, request, 0)
        first_result = {"source_revision": task["source_revision"], "sections": [{
            "text": "Первый блок.", "evidence": [word["word_id"] for word in first["task"]["words"]]}]}
        workflow.accept_model_part(self.store, request, 0, first_result)

        class FailingClient:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def generate(self, *args, **kwargs):
                raise llm_provider.ProviderError("offline", "OpenCode или OpenAI временно недоступен")

        with mock.patch("llm_provider.OpenCodeTextClient", FailingClient):
            with self.assertRaises(llm_provider.ProviderError):
                workflow.execute_model(self.store, request)
        second_words = task["words"][workflow.MODEL_PART_WORDS:]
        answer = {"source_revision": task["source_revision"], "sections": [{
            "text": "Второй блок.", "evidence": [word["word_id"] for word in second_words]}]}

        class CompletingClient:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def generate(self, *_args, **_kwargs): return answer, '{"ok":true}'

        with mock.patch("llm_provider.OpenCodeTextClient", CompletingClient):
            result = workflow.execute_model(self.store, request, retry_failed=True)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM workflow_model_parts").fetchone()[0], 2)

    def test_no_cloud_request_without_a_required_conspect(self):
        request = workflow.create(self.store, self.source)
        with self.assertRaises(storage.Conflict):
            workflow.execute_model(self.store, request)

    def test_cloud_disabled_policy_makes_no_provider_call(self):
        request = self.prepare_request(allow_cloud=False)
        with mock.patch("llm_provider.OpenCodeTextClient") as client:
            with self.assertRaisesRegex(storage.Conflict, "облачный"):
                workflow.execute_model(self.store, request)
        client.assert_not_called()

    def test_invalid_answer_has_exactly_one_repair_then_fails_honestly(self):
        request = self.prepare_request()

        class InvalidClient:
            calls = 0
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def generate(self, *_args, **_kwargs):
                self.calls += 1
                return {"source_revision": 1, "sections": [{"text": "Итог.", "evidence": ["unknown"]}]}, "invalid"

        client = InvalidClient()
        with mock.patch("llm_provider.OpenCodeTextClient", return_value=client):
            with self.assertRaises(llm_provider.ProviderError) as error:
                workflow.execute_model(self.store, request)
        self.assertEqual(error.exception.code, "invalid_response")
        self.assertEqual(client.calls, 2)
        saved = workflow.get(self.store, request)
        self.assertEqual(saved["state"], "failed")
        self.assertEqual(saved["model_attempts"], 2)

    def test_retry_failed_resets_invalid_response_repair_budget(self):
        request = self.prepare_request()
        task = workflow.prepare_model_task(self.store, request)
        word_id = task["words"][0]["word_id"]
        invalid = {"source_revision": task["source_revision"],
                   "sections": [{"text": "Итог.", "evidence": ["unknown"]}]}
        for _ in range(2):
            with self.assertRaises(ValueError):
                workflow.accept_model_result(self.store, request, invalid)
        self.assertEqual(workflow.get(self.store, request)["state"], "failed")

        valid = {"source_revision": task["source_revision"],
                 "sections": [{"text": "Краткий итог.", "evidence": [word_id]}]}

        class RepairingClient:
            calls = 0

            def __enter__(self): return self
            def __exit__(self, *args): return False

            def generate(self, *_args, **kwargs):
                self.calls += 1
                return (invalid, '{"bad":true}') if kwargs.get("repair_error") is None \
                    else (valid, '{"ok":true}')

        client = RepairingClient()
        with mock.patch("llm_provider.OpenCodeTextClient", return_value=client):
            result = workflow.execute_model(self.store, request, retry_failed=True)
        self.assertEqual(result["state"], "completed")
        self.assertEqual(client.calls, 2)
