# -*- coding: utf-8 -*-
"""无 API 单元测试：filetrans 异步转写的 prepare/generate 令牌门禁、解析与输出。"""

import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import wave
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import asr_qwen  # noqa: E402


AUDIO_URL = "https://example.com/meeting.wav"
RESULT_URL = "https://result.example.com/transcription.json"
REQUEST = "转写会议录音并区分说话人"
PLAN = "单文件自动说话人分离，输出带时间戳的 TXT+JSON"
EXPLANATION = ("按需求启用自动说话人分离并锁定人数，补充语言提示与输出格式，"
               "保证结果可读且结构化。")


def make_wav(path, seconds=1.0, rate=16000):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return str(path)


def prepare_args(audio, **overrides):
    base = dict(
        request=REQUEST,
        optimized_plan=PLAN,
        change_explanation=EXPLANATION,
        audio=audio,
        speaker_count=2,
        language_hints="zh",
        output=None,
    )
    base.update(overrides)
    return Namespace(**base)


def generate_args(token, audio, plan=PLAN, output=None):
    return Namespace(token=token, audio=audio, optimized_plan=plan, output=output)


SUBMIT_DOC = {
    "request_id": "req-submit-001",
    "output": {"task_id": "task-001"},
}
PENDING_DOC = {
    "request_id": "req-query-001",
    "output": {"task_id": "task-001", "task_status": "PENDING", "results": []},
}
SUCCEEDED_DOC = {
    "request_id": "req-query-002",
    "output": {
        "task_id": "task-001",
        "task_status": "SUCCEEDED",
        "results": [
            {
                "subtask_status": "SUCCEEDED",
                "transcription_url": RESULT_URL,
                "task_metrics": {"audio_duration": 2.6},
            }
        ],
    },
}
RESULT_DOC = {
    "file_url": AUDIO_URL,
    "properties": {
        "audio_format": "pcm_s16le",
        "channels": [0],
        "original_sampling_rate": 16000,
        "original_duration_in_milliseconds": 2600,
    },
    "transcripts": [
        {
            "channel_id": 0,
            "content_duration_in_milliseconds": 2480,
            "text": "大家好，今天汇报开始。好的，请开始。",
            "sentences": [
                {
                    "begin_time": 0,
                    "end_time": 1200,
                    "text": "大家好，今天汇报开始。",
                    "speaker_id": 0,
                    "words": [
                        {"begin_time": 0, "end_time": 400, "text": "大家好", "punctuation": "，"},
                    ],
                },
                {
                    "begin_time": 1300,
                    "end_time": 2600,
                    "text": "好的，请开始。",
                    "speaker_id": 1,
                    "words": [],
                },
            ],
        }
    ],
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self.payload = payload or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeTransport:
    def __init__(self):
        self.post_calls = []
        self.get_urls = []
        self.query_count = 0

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return FakeResponse(200, SUBMIT_DOC)

    def get(self, url, **kwargs):
        self.get_urls.append(url)
        if url == RESULT_URL:
            return FakeResponse(200, RESULT_DOC)
        self.query_count += 1
        if self.query_count == 1:
            return FakeResponse(200, PENDING_DOC)
        return FakeResponse(200, SUCCEEDED_DOC)


class AsrSkillTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="asr_qwen_test_"))
        self.pending_name = f"skill_asr_qwen_pending_{os.getpid()}_{int(time.time() * 1000)}"
        self.original_pending_name = asr_qwen.PENDING_DIRNAME
        asr_qwen.PENDING_DIRNAME = self.pending_name

    def tearDown(self):
        asr_qwen.PENDING_DIRNAME = self.original_pending_name
        pending = Path(tempfile.gettempdir()) / self.pending_name
        if pending.exists():
            shutil.rmtree(pending, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_prepare(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            asr_qwen.cmd_prepare(args)
        return out.getvalue(), err.getvalue()

    def expect_die(self, fn, needle):
        err = io.StringIO()
        with redirect_stderr(err):
            with self.assertRaises(SystemExit):
                fn()
        self.assertIn(needle, err.getvalue())

    def record_files(self):
        return list((Path(tempfile.gettempdir()) / self.pending_name).glob("*.json"))

    def run_generate_mocked(self, args, audio_url=None, upload_oss=None):
        transport = FakeTransport()
        with mock.patch.dict(os.environ, {
            "DASHSCOPE_API_KEY": "test-placeholder",
            "DASHSCOPE_WORKSPACE_ID": "test-placeholder",
        }):
            with mock.patch.object(asr_qwen.requests, "post", transport.post):
                with mock.patch.object(asr_qwen.requests, "get", transport.get):
                    if upload_oss is not None:
                        with mock.patch.object(asr_qwen, "upload_local_to_oss",
                                               return_value=upload_oss):
                            out = io.StringIO()
                            with redirect_stdout(out):
                                asr_qwen.cmd_generate(args)
                    else:
                        out = io.StringIO()
                        with redirect_stdout(out):
                            asr_qwen.cmd_generate(args)
        return transport, out.getvalue()


class TestPrepareValidations(AsrSkillTestBase):
    def test_missing_local_audio_rejected(self):
        args = prepare_args(str(self.tmp / "missing.wav"))
        self.expect_die(lambda: asr_qwen.cmd_prepare(args), "音频文件不存在")

    def test_empty_local_audio_rejected(self):
        empty = self.tmp / "empty.wav"
        empty.write_bytes(b"")
        args = prepare_args(str(empty))
        self.expect_die(lambda: asr_qwen.cmd_prepare(args), "音频文件为空")

    def test_short_explanation_rejected(self):
        args = prepare_args(AUDIO_URL, change_explanation="太短")
        self.expect_die(lambda: asr_qwen.cmd_prepare(args), "改动说明过于简略")

    def test_invalid_speaker_count_rejected(self):
        args = prepare_args(AUDIO_URL, speaker_count=1)
        self.expect_die(lambda: asr_qwen.cmd_prepare(args), "取值范围")

    def test_too_many_language_hints_rejected(self):
        args = prepare_args(AUDIO_URL, language_hints="zh,en,ja,ko,vi")
        self.expect_die(lambda: asr_qwen.cmd_prepare(args), "最多 4 个")


class TestPrepareSuccess(AsrSkillTestBase):
    def test_prepare_url_creates_token_record(self):
        out, _ = self.run_prepare(prepare_args(AUDIO_URL))
        files = self.record_files()
        self.assertEqual(len(files), 1)
        token = files[0].stem
        self.assertEqual(len(token), 73)
        self.assertIn("=== 待确认转写方案", out)
        self.assertIn("=== 确认令牌（Token A）===", out)
        self.assertIn(token, out)
        rec = asr_qwen.load_record(token)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["token"], token)
        self.assertEqual(rec["audio"], AUDIO_URL)
        self.assertEqual(rec["params"]["speaker_count"], 2)
        self.assertEqual(rec["params"]["language_hints"], ["zh"])
        self.assertIsNone(rec["duration_seconds"])
        self.assertIsNone(rec["price_estimate"])

    def test_prepare_local_wav_estimates_duration_and_price(self):
        audio = make_wav(self.tmp / "meeting.wav", seconds=1.0)
        _, _ = self.run_prepare(prepare_args(audio))
        token = self.record_files()[0].stem
        rec = asr_qwen.load_record(token)
        self.assertAlmostEqual(rec["duration_seconds"], 1.0, places=1)
        self.assertAlmostEqual(rec["price_estimate"], asr_qwen.PRICE_PER_SECOND, places=5)

    def test_prepare_oss_url_accepted(self):
        oss_url = "oss://dashscope-instant/probe/meeting.wav"
        _, _ = self.run_prepare(prepare_args(oss_url))
        token = self.record_files()[0].stem
        rec = asr_qwen.load_record(token)
        self.assertEqual(rec["audio"], oss_url)
        self.assertIsNone(rec["duration_seconds"])


class TestTokenLifecycle(AsrSkillTestBase):
    def test_cleanup_expired_removes_old_records(self):
        token = asr_qwen.make_token("0" * 64)
        rp = asr_qwen.save_record(token, {"stage": "prepare", "fingerprint": "0" * 64, "audio": AUDIO_URL})
        old = time.time() - asr_qwen.RECORD_TTL_SECONDS - 60
        os.utime(rp, (old, old))
        asr_qwen.cleanup_expired()
        self.assertFalse(rp.exists())


class TestGenerateRejections(AsrSkillTestBase):
    def test_no_token_record_rejected(self):
        token = "0" * 64 + "-" + "a" * 8
        args = generate_args(token, AUDIO_URL)
        self.expect_die(lambda: asr_qwen.cmd_generate(args), "无待确认记录")

    def test_audio_mismatch_rejected(self):
        _, _ = self.run_prepare(prepare_args(AUDIO_URL))
        token = self.record_files()[0].stem
        args = generate_args(token, "https://example.com/other.wav")
        self.expect_die(lambda: asr_qwen.cmd_generate(args), "不一致")

    def test_plan_mismatch_rejected(self):
        _, _ = self.run_prepare(prepare_args(AUDIO_URL))
        token = self.record_files()[0].stem
        args = generate_args(token, AUDIO_URL, plan="被篡改的方案")
        self.expect_die(lambda: asr_qwen.cmd_generate(args), "不一致")


class TestGenerateSuccessMocked(AsrSkillTestBase):
    def test_generate_url_success_destroys_token_and_writes_outputs(self):
        out_base = str(self.tmp / "result")
        _, _ = self.run_prepare(prepare_args(AUDIO_URL))
        token = self.record_files()[0].stem
        args = generate_args(token, AUDIO_URL, output=out_base)
        transport, out = self.run_generate_mocked(args)

        self.assertFalse(self.record_files(), "generate 成功后令牌A应已销毁")
        self.assertEqual(len(transport.post_calls), 1)
        submit_url, submit_kwargs = transport.post_calls[0]
        self.assertIn("/services/audio/asr/transcription", submit_url)
        self.assertEqual(submit_kwargs["headers"]["X-DashScope-Async"], "enable")
        self.assertNotIn("X-DashScope-OssResourceResolve", submit_kwargs["headers"])
        payload = submit_kwargs["json"]
        self.assertEqual(payload["model"], asr_qwen.MODEL)
        self.assertEqual(payload["input"]["file_urls"], [AUDIO_URL])
        params = payload["parameters"]
        self.assertEqual(params["channel_id"], [0])
        self.assertTrue(params["diarization_enabled"])
        self.assertEqual(params["speaker_count"], 2)
        self.assertEqual(params["language_hints"], ["zh"])

        txt_path = Path(out_base + ".txt")
        json_path = Path(out_base + ".json")
        self.assertTrue(txt_path.exists())
        self.assertTrue(json_path.exists())
        txt = txt_path.read_text(encoding="utf-8")
        self.assertIn("说话人1：大家好，今天汇报开始。", txt)
        self.assertIn("说话人2：好的，请开始。", txt)
        self.assertIn("[00:00.000 - 00:01.200]", txt)
        data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(data["meta"]["task_id"], "task-001")
        self.assertEqual(data["meta"]["speaker_count"], 2)
        self.assertEqual(len(data["sentences"]), 2)
        self.assertEqual(data["sentences"][0]["speaker"], "说话人1")
        self.assertEqual(data["sentences"][0]["speaker_id"], 0)
        self.assertIn("令牌A已销毁", out)

    def test_generate_local_uploads_oss_and_submits(self):
        audio = make_wav(self.tmp / "meeting.wav", seconds=1.0)
        out_base = str(self.tmp / "result")
        oss_url = "oss://dashscope-instant/probe/meeting.wav"
        _, _ = self.run_prepare(prepare_args(audio))
        token = self.record_files()[0].stem
        args = generate_args(token, audio, output=out_base)
        transport, out = self.run_generate_mocked(args, upload_oss=oss_url)

        self.assertIn(f"上传完成：{oss_url}", out)
        submit_url, submit_kwargs = transport.post_calls[0]
        self.assertEqual(submit_kwargs["headers"]["X-DashScope-OssResourceResolve"], "enable")
        payload = submit_kwargs["json"]
        self.assertEqual(payload["input"]["file_urls"], [oss_url])
        self.assertTrue(Path(out_base + ".txt").exists())
        self.assertFalse(self.record_files())


class TestParseAndRender(AsrSkillTestBase):
    def test_speaker_zero_based_mapping_and_timestamp(self):
        sentences = [
            {"speaker_id": 0, "begin_time": 0, "end_time": 1000, "text": "第一句"},
            {"begin_time": 1100, "end_time": 2000, "text": "第二句"},
        ]
        parsed = asr_qwen.parse_sentences(sentences)
        self.assertEqual(parsed[0]["speaker"], "说话人1")
        self.assertEqual(parsed[0]["speaker_id"], 0)
        self.assertEqual(parsed[1]["speaker"], "说话人2")
        self.assertEqual(asr_qwen.render_timestamp(170), "00:00.170")
        self.assertEqual(asr_qwen.render_timestamp(61234), "01:01.234")
        self.assertIsNone(asr_qwen.render_timestamp(-1))

    def test_parse_result_document_flattens_transcripts(self):
        sentences, duration = asr_qwen.parse_result_document(RESULT_DOC)
        self.assertEqual(len(sentences), 2)
        self.assertAlmostEqual(duration, 2.48, places=2)

    def test_render_txt_skips_bracket_without_times(self):
        sentences = [{"speaker": "说话人1", "begin_time": None, "end_time": None, "text": "无时间戳"}]
        self.assertEqual(asr_qwen.render_txt(sentences), "说话人1：无时间戳\n")

    def test_canonical_spec_stable(self):
        spec1 = asr_qwen.canonical_spec("r", "p", "c", AUDIO_URL, 2, ["zh"], "out")
        spec2 = asr_qwen.canonical_spec("r", "p", "c", AUDIO_URL, 2, ["zh"], "out")
        self.assertEqual(asr_qwen.compute_fingerprint(spec1),
                         asr_qwen.compute_fingerprint(spec2))


if __name__ == "__main__":
    unittest.main()
