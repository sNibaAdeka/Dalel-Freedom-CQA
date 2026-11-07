"""Behavior checks for the private UI fixture generator, using authored data only."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/make_ui_mocks.py"
spec = importlib.util.spec_from_file_location("ui_mocks", SCRIPT)
mocks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mocks)


def js_slice(value, start, end):
    return value.encode("utf-16-le")[start * 2:end * 2].decode("utf-16-le")


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = mocks.synthetic_records()
        cls.files = mocks.build_fixtures(cls.records, synthetic=True)

    def test_every_row_has_detail_and_audio_response(self):
        rows = self.files["conversations.json"]
        self.assertEqual(len(rows), 65)
        for row in rows:
            detail = self.files[f"conversation_{row['id']}.json"]
            self.assertEqual(len(row["cells"]), 8)
            self.assertEqual(detail["score"]["total"], row["total"])
            self.assertIn(f"audio_{row['id']}.json", self.files)
            self.assertEqual(detail["id"], row["id"])
            self.assertNotIn("conversation", detail)

    def test_six_requested_demo_categories(self):
        demo_ids = self.files["manifest.json"]["demo_ids"]
        self.assertEqual(len(demo_ids), 6)
        chosen = [self.files[f"conversation_{did}.json"] for did in demo_ids]
        self.assertEqual(sum(d["channel"] == "call" for d in chosen), 2)
        self.assertEqual(sum(d["language"] == "KZ" for d in chosen), 1)
        kz = next(d for d in chosen if d["language"] == "KZ")
        self.assertTrue(any(e["answer"] == "na" and e["na_reason"] for e in kz["evals"]))

    def test_exact_quotes_operator_roles_and_original_timestamps(self):
        for source in self.records:
            detail = self.files[f"conversation_{source['conversation_id']}.json"]
            by_id = {message["id"]: message for message in detail["messages"]}
            self.assertEqual([t["timestamp"] for t in source["transcript"]], [m["ts_raw"] for m in detail["messages"]])
            for evaluation in detail["evals"]:
                for evidence in evaluation["evidence"]:
                    message = by_id[evidence["message_id"]]
                    self.assertEqual(message["speaker"], "operator")
                    self.assertEqual(js_slice(message["text"], evidence["start"], evidence["end"]), evidence["quote"])
                    self.assertTrue(evidence["quote"])
                if evaluation["checked_range"]:
                    start, end = (by_id[mid] for mid in evaluation["checked_range"])
                    self.assertLessEqual(start["idx"], end["idx"])
                    self.assertEqual(start["speaker"], "operator")
                    self.assertEqual(end["speaker"], "operator")

    def test_utf16_offsets_with_non_bmp_text(self):
        message = {"id": "unicode:0", "text": "  🧭 Учебный пример с точной цитатой.  "}
        evidence = mocks.evidence_for(message)
        self.assertEqual(evidence["start"], 2)
        self.assertEqual(js_slice(message["text"], evidence["start"], evidence["end"]), evidence["quote"])
        self.assertGreater(evidence["end"] - evidence["start"], len(evidence["quote"]))

    def test_scores_reconcile_and_na_weights_redistribute(self):
        for row in self.files["conversations.json"]:
            detail = self.files[f"conversation_{row['id']}.json"]
            score = detail["score"]
            self.assertAlmostEqual(sum(line["weight_effective"] for line in score["lines"]), 100)
            self.assertAlmostEqual(sum(line["contribution"] or 0 for line in score["lines"]), score["total_raw"])
            self.assertEqual(mocks.rounded(score["total_raw"], 2), score["total"])
            for line in score["lines"]:
                if line["is_na"]:
                    self.assertEqual(line["weight_effective"], 0)
                    self.assertIsNone(line["points"])
                else:
                    self.assertGreaterEqual(line["points"], 0)
                    self.assertLessEqual(line["points"], line["max_points"])

    def test_applied_decision_has_computed_delta_and_immutable_source(self):
        decision = self.files["decision_result.json"]
        detail = self.files[f"conversation_{decision['conv_id']}.json"]
        self.assertEqual(decision["old_total"], detail["score"]["total"])
        changed = copy.deepcopy(detail["evals"])
        changed[1]["answer"] = decision["decision"]["new_answer"]
        expected = mocks.score(detail["id"], detail["checklist"], changed)
        self.assertEqual(expected["total"], decision["new_total"])
        self.assertNotEqual(decision["old_total"], decision["new_total"])
        self.assertEqual(detail["decisions"], [])
        self.assertEqual(detail["appeals"][0]["evaluation_id"], detail["evals"][1]["evaluation_id"])

    def test_recording_pause_is_subtracted_and_system_stays_system(self):
        call = next(r for r in self.records if r["channel"] == "voice call")
        detail = self.files[f"conversation_{call['conversation_id']}.json"]
        self.assertEqual(detail["meta"]["recording_gaps"], [[43, 63]])
        pause = next(m for m in detail["messages"] if "приостановлены" in m["text"])
        self.assertEqual(pause["speaker"], "system")
        self.assertIsNone(pause["rec_sec"])
        after = next(m for m in detail["messages"] if m["t_sec"] == 68)
        self.assertEqual(after["rec_sec"], 48)

    def test_signed_url_and_unrelated_source_metadata_do_not_escape(self):
        record = copy.deepcopy(self.records[-1])
        record["audio_url"] = "https://private.invalid/recording?sig=SECRET_TEST"
        record["private_token"] = "SECRET_TEST"
        files = mocks.build_fixtures([record])
        self.assertNotIn("SECRET_TEST", json.dumps(files))
        detail = files[f"conversation_{record['conversation_id']}.json"]
        self.assertIsNone(detail["audio_url"])
        self.assertIsNone(files[f"audio_{record['conversation_id']}.json"]["src"])

    def test_only_changed_criterion_has_new_hash(self):
        first, second = self.files["checklists.json"]
        changed = [a["id"] for a, b in zip(first["criteria"], second["criteria"]) if a["hash"] != b["hash"]]
        self.assertEqual(changed, ["C4"])
        self.assertEqual(sum(c["weight"] for c in first["criteria"]), 100)

    def test_run_progress_monotonic_and_final_scores_match_v2(self):
        previous = -1
        for index in range(5):
            progress = self.files[f"run_progress_{index}.json"]
            self.assertGreater(progress["done"], previous)
            self.assertEqual(progress["from_cache"], progress["done"] * 7)
            self.assertEqual(sum(p["status"] == "complete" for p in progress["per_conv"]), progress["done"])
            previous = progress["done"]
        self.assertEqual(previous, 65)
        self.assertEqual(self.files["run_progress_4.json"]["status"], "complete")

    def test_no_operator_means_incomplete_not_a_performance_score(self):
        record = copy.deepcopy(self.records[0])
        record["transcript"] = [m for m in record["transcript"] if m["speaker"] != "operator"]
        files = mocks.build_fixtures([record])
        detail = files[f"conversation_{record['conversation_id']}.json"]
        self.assertIsNone(detail["score"]["total"])
        self.assertEqual(detail["score"]["status"], "incomplete")
        self.assertTrue(all(e["needs_review"] for e in detail["evals"]))

    def test_envelope_duplicate_and_unsafe_ids(self):
        self.assertEqual(mocks.unpack_records({"isSuccess": True, "data": {"records": self.records}}), self.records)
        with self.assertRaises(ValueError):
            mocks.build_fixtures([self.records[0], self.records[0]])
        record = copy.deepcopy(self.records[0])
        record["conversation_id"] = "../../escape"
        with self.assertRaises(ValueError):
            mocks.build_fixtures([record])

    def test_official_v1_control_example_and_flags(self):
        checklist = mocks.make_checklist()
        answers = dict(C1='yes', C2='na', C3='na', C4=15, C5='yes', C6=7, C7='yes', C8='no')
        evaluations = [{"criterion_id": key, "answer": value, "critical_hit": True} for key, value in answers.items()]
        result = mocks.score('control', checklist, evaluations)
        self.assertEqual(result['total'], 85.38)
        self.assertFalse(result['critical'])
        self.assertEqual(result['lines'][5]['points'], 10.5)
        self.assertEqual(len(result['sections']), 4)
        self.assertIsNone(result['sections']['Грубые нарушения'])
        evaluations[-1]['answer'] = 'yes'
        self.assertTrue(mocks.score('control', checklist, evaluations)['critical'])

    def test_all_5184_official_combinations_against_independent_oracle(self):
        from frontend_v1_oracle import cases
        checklist = mocks.make_checklist()
        count = 0
        for case in cases():
            actual = mocks.score('oracle', checklist, [{"criterion_id": k, "answer": v} for k, v in case['answers'].items()])
            self.assertEqual(actual['total'], case['total'], case['answers'])
            self.assertEqual(actual['critical'], case['critical'], case['answers'])
            count += 1
        self.assertEqual(count, 5184)

    def test_no_applicable_and_arbitrary_weight_total(self):
        checklist = mocks.make_checklist()
        checklist['criteria'] = [c for c in checklist['criteria'] if c['na_allowed']]
        result = mocks.score('empty', checklist, [{'criterion_id': c['id'], 'answer': 'na'} for c in checklist['criteria']])
        self.assertIsNone(result['total'])
        self.assertEqual(result['status'], 'no_applicable')
        checklist['criteria'][0]['weight'] = 37.5
        result = mocks.score('weights', checklist, [{'criterion_id': c['id'], 'answer': max(c['scale']) if c['scale'] else 'yes'} for c in checklist['criteria']])
        self.assertEqual(result['total'], 100)

    def test_delayed_recording_start_is_not_conversation_start(self):
        source = {'conversation_id': 'late', 'language': 'RU', 'channel': 'voice call', 'transcript': [
            {'timestamp': '00:00', 'speaker': 'system', 'text': 'Соединение.'},
            {'timestamp': '00:30', 'speaker': 'system', 'text': 'Запись и транскрибирование запущены.'},
            {'timestamp': '00:40', 'speaker': 'operator', 'text': 'Учебное приветствие.'}]}
        normalized = mocks.normalize(source)
        self.assertIsNone(normalized['messages'][0]['rec_sec'])
        self.assertEqual(normalized['messages'][-1]['rec_sec'], 10)

    def test_regeneration_removes_obsolete_ids_preserves_unrelated_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            mocks.write_fixtures(self.files, path)
            (path / "keep.txt").write_text("unrelated")
            smaller = mocks.build_fixtures(self.records[:1], synthetic=True)
            mocks.write_fixtures(smaller, path)
            old_id = self.records[-1]["conversation_id"]
            self.assertFalse((path / f"conversation_{old_id}.json").exists())
            self.assertEqual((path / "keep.txt").read_text(), "unrelated")
            for file in path.glob("*.json"):
                json.loads(file.read_text())


if __name__ == "__main__":
    unittest.main()
