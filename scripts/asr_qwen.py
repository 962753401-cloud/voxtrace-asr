#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""asr_qwen.py - 阿里云百炼 Qwen-Audio-3.0-ASR-Flash-Filetrans 录音文件异步转写。

两阶段令牌链条（代码兜底，生成前必须确认方案）：
  - prepare：登记「原始需求 -> 优化后方案 -> 改动说明」并锁定音频与转写参数，
             生成令牌A（绑定内容指纹 sha256），落盘待确认记录；绝不调用真实 API。
  - generate：三道检查（记录存在 / 令牌匹配 / 内容未被篡改）通过后才提交异步
             转写任务，成功后立即销毁令牌A（用完即焚）。

调用方式（官方最佳实践）：
  - 公网 HTTP/HTTPS 音频 URL 直接作为 file_urls 提交；
  - 本地音频在 generate 阶段先上传到百炼临时 OSS，再用 RESTful API 提交。
    SDK 的 Transcription 不支持 oss:// 前缀临时 URL，因此脚本统一走 RESTful 流程。
  - 任务流程：POST /services/audio/asr/transcription -> GET /tasks/{task_id} 轮询
              -> 下载 output.results[].transcription_url 指向的完整结果 JSON。

令牌与内容绑定：指纹覆盖 request/optimized_plan/change_explanation/音频标识/
speaker_count/language_hints/output，提交内容与确认版本不一致即拒绝。
令牌是流程副产品，由代码动态生成，非写死密码；报错文案即流程指南。
"""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from http import HTTPStatus
from pathlib import Path

import requests

MODEL = "qwen-audio-3.0-asr-flash-filetrans"
PRICE_PER_SECOND = 0.00022  # 元/秒（华北2北京单价；国际地域 0.00026 元/秒）
PRICE_PER_SECOND_INTL = 0.00026

SPEAKER_COUNT_MIN = 2
SPEAKER_COUNT_MAX = 100  # 官方参数范围 [2,100]
LANGUAGE_HINTS_MAX = 4
CHANNEL_ID = [0]
TASK_POLL_INTERVAL_SECONDS = 2
TASK_WAIT_TIMEOUT_SECONDS = 2 * 60 * 60
RECORD_TTL_SECONDS = 2 * 60 * 60  # 2 小时
PENDING_DIRNAME = "skill_asr_qwen_pending"  # 令牌A（待确认）记录目录
MIN_CHANGE_EXPLANATION_LEN = 20  # 防敷衍：改动说明最短字数


class TranscriptionApiError(Exception):
    """转写任务提交/轮询/下载失败。"""


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def ensure_utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def read_value(value):
    """支持 @文件 语法：值以 @ 开头时从文件读取内容（应对长文本）。"""
    if value.startswith("@"):
        path = Path(value[1:])
        if not path.is_file():
            die(f"错误：@文件 不存在：{path}")
        return path.read_text(encoding="utf-8").strip()
    return value


def is_http_url(value):
    return str(value).startswith(("http://", "https://"))


def is_oss_url(value):
    return str(value).startswith("oss://")


def is_remote_audio(value):
    return is_http_url(value) or is_oss_url(value)


def normalize_audio(value):
    """规范化音频：公网/OSS URL 原样返回；本地路径校验存在、非空。"""
    if not value or not str(value).strip():
        die("错误：--audio 音频文件路径或 URL 不能为空。")
    value = str(value).strip()
    if is_remote_audio(value):
        return value
    path = Path(value).resolve()
    if not path.is_file():
        die(f"错误：音频文件不存在：{path}")
    if path.stat().st_size <= 0:
        die(f"错误：音频文件为空（0 字节）：{path}")
    return str(path)


# ============================================================
# 指纹与令牌记录（两段式链条核心）
# ============================================================

def canonical_spec(request, optimized_plan, change_explanation, audio,
                   speaker_count, language_hints, output):
    """待确认方案的稳定序列化（用于指纹）。sort_keys 保证字段顺序稳定。"""
    return json.dumps(
        {
            "request": request,
            "optimized_plan": optimized_plan,
            "change_explanation": change_explanation,
            "audio": audio,
            "speaker_count": speaker_count,
            "language_hints": language_hints,
            "output": output,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def compute_fingerprint(spec):
    """方案内容指纹 = sha256(spec)（UTF-8）。"""
    return hashlib.sha256(spec.encode("utf-8")).hexdigest()


def make_token(fingerprint):
    """令牌 = 指纹-随机nonce。nonce 保证同方案多次 prepare 不互相覆盖。"""
    return f"{fingerprint}-{secrets.token_hex(4)}"


def pending_dir():
    """待确认记录目录（系统临时目录下，保证可写）。"""
    d = Path(tempfile.gettempdir()) / PENDING_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def record_path(token):
    """根据令牌计算记录文件路径；令牌只允许十六进制与连字符，防路径穿越。"""
    allowed = set("0123456789abcdef-")
    if not token or any(c not in allowed for c in token):
        return None
    return pending_dir() / f"{token}.json"


def save_record(token, data):
    rec = {**data, "token": token, "created_at": time.time()}
    rp = record_path(token)
    if rp is None:
        die("错误：令牌格式非法，无法落盘记录。")
    rp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    return rp


def load_record(token):
    rp = record_path(token)
    if rp is None or not rp.is_file():
        return None
    try:
        return json.loads(rp.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None


def destroy_record(token):
    """销毁令牌记录（用完即焚）。"""
    rp = record_path(token)
    if rp and rp.is_file():
        try:
            rp.unlink()
        except OSError:
            pass


def cleanup_expired():
    """删除超过 RECORD_TTL_SECONDS 的待确认记录，防止堆积。"""
    d = pending_dir()
    cutoff = time.time() - RECORD_TTL_SECONDS
    for f in d.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# ============================================================
# 音频时长与费用估算
# ============================================================

def detect_duration(audio):
    """返回 (时长秒, 来源)。优先 ffprobe，失败时用 wave 解析 WAV，再失败返回 (None, 'unknown')。"""
    if is_remote_audio(audio):
        return None, "url"
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=nw=1:nk=1", audio],
                capture_output=True, text=True, timeout=15,
            )
            text = proc.stdout.strip()
            if proc.returncode == 0 and text:
                return float(text), "ffprobe"
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if Path(audio).suffix.lower() == ".wav":
        try:
            with wave.open(audio, "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                if rate > 0:
                    return frames / rate, "wave"
        except (OSError, wave.Error, EOFError):
            pass
    return None, "unknown"


def estimate_price(duration):
    if duration is None:
        return None
    return duration * PRICE_PER_SECOND


# ============================================================
# 结果解析与输出
# ============================================================

def render_timestamp(ms):
    """毫秒 -> mm:ss.mmm。"""
    if not isinstance(ms, (int, float)) or ms < 0:
        return None
    ms = int(ms)
    mm, rest = divmod(ms, 60000)
    ss, mmm = divmod(rest, 1000)
    return f"{mm:02d}:{ss:02d}.{mmm:03d}"


def parse_sentences(sentences):
    """把结果 JSON 的句子列表解析为结构化记录。

    filetrans 的 speaker_id 以 0 起始，输出统一转成 说话人1/2/…；
    缺失 speaker_id 时按出现顺序回退。
    """
    out = []
    if not isinstance(sentences, list):
        return out
    for idx, s in enumerate(sentences):
        if not isinstance(s, dict):
            continue
        text = s.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        raw = s.get("speaker_id")
        if raw is None or raw == "":
            speaker = f"说话人{idx + 1}"
        elif str(raw).lstrip("-").isdigit():
            speaker = f"说话人{int(raw) + 1}"
        else:
            speaker = str(raw)
        out.append({
            "speaker": speaker,
            "speaker_id": raw,
            "begin_time": s.get("begin_time"),
            "end_time": s.get("end_time"),
            "text": text,
            "confidence": s.get("confidence"),
            "words": s.get("words") or [],
        })
    return out


def parse_result_document(doc):
    """解析 transcription_url 下载的完整结果 JSON，返回 (句子列表, 语音内容秒数)。"""
    sentences = []
    duration_ms = None
    transcripts = doc.get("transcripts") or []
    for tr in transcripts:
        if not isinstance(tr, dict):
            continue
        sentences.extend(parse_sentences(tr.get("sentences") or []))
        cd = tr.get("content_duration_in_milliseconds")
        if isinstance(cd, (int, float)):
            duration_ms = (duration_ms or 0) + float(cd)
    if duration_ms is None:
        props = doc.get("properties") or {}
        original = props.get("original_duration_in_milliseconds")
        if isinstance(original, (int, float)):
            duration_ms = float(original)
    duration = duration_ms / 1000.0 if duration_ms is not None else None
    return sentences, duration


def render_txt(sentences):
    lines = []
    for s in sentences:
        begin = render_timestamp(s.get("begin_time"))
        end = render_timestamp(s.get("end_time"))
        if begin and end:
            lines.append(f"[{begin} - {end}] {s['speaker']}：{s['text']}")
        else:
            lines.append(f"{s['speaker']}：{s['text']}")
    return "\n".join(lines) + ("\n" if lines else "")


def write_outputs(out_base, meta, sentences):
    """按基础名写出 .txt 与 .json，返回两个绝对路径。"""
    base = Path(str(out_base))
    base.parent.mkdir(parents=True, exist_ok=True)
    txt_path = Path(str(base) + ".txt").resolve()
    json_path = Path(str(base) + ".json").resolve()
    txt_path.write_text(render_txt(sentences), encoding="utf-8")
    json_path.write_text(
        json.dumps({"meta": meta, "sentences": sentences}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return txt_path, json_path


def need_key():
    key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
    if not key:
        die("错误：未设置环境变量 DASHSCOPE_API_KEY。\n"
            "PowerShell 临时：  $env:DASHSCOPE_API_KEY = '替换为你的APIKey'\n"
            "持久：  setx DASHSCOPE_API_KEY '替换为你的APIKey'  后重开终端。\n"
            "切勿将密钥写入技能文件或在聊天中明文粘贴。")
    return key


def need_workspace():
    ws = os.environ.get("DASHSCOPE_WORKSPACE_ID", "").strip()
    if not ws:
        die("错误：未设置环境变量 DASHSCOPE_WORKSPACE_ID。\n"
            "PowerShell 临时：  $env:DASHSCOPE_WORKSPACE_ID = '替换为你的WorkspaceId'\n"
            "持久：  setx DASHSCOPE_WORKSPACE_ID '替换为你的WorkspaceId'  后重开终端。")
    return ws


def default_output():
    ts = time.strftime("%Y%m%d_%H%M%S")
    return str(Path("output") / f"asr_{ts}")


def validate_plan_fields(request, optimized_plan, change_explanation):
    if not request.strip():
        die("错误：--request（原始需求）不能为空。prepare 环节必须登记原始需求，便于用户对比确认。")
    if not optimized_plan.strip():
        die("错误：--optimized-plan（审核优化后的转写方案）不能为空。")
    if not change_explanation.strip():
        die("错误：--change-explanation（改动说明）不能为空。\n"
            "防敷衍要求：必须说明优化了哪些方面、为什么（如：锁定说话人分离参数、"
            "明确语言提示、规范输出格式等）。")
    if len(change_explanation.strip()) < MIN_CHANGE_EXPLANATION_LEN:
        die(f"错误：改动说明过于简略（少于 {MIN_CHANGE_EXPLANATION_LEN} 字），请具体说明"
            "优化了哪些方面、为什么。这是防敷衍作业，也是用户确认的依据。")


def parse_language_hints(value):
    """把 'zh,en' 转成列表；最多 4 个（官方上限），至少 1 个。"""
    hints = [x.strip() for x in str(value).replace("，", ",").split(",") if x.strip()]
    if not hints:
        die("错误：--language-hints（语言提示）不能为空，默认 zh。")
    if len(hints) > LANGUAGE_HINTS_MAX:
        die(f"错误：--language-hints 最多 {LANGUAGE_HINTS_MAX} 个值，当前 {len(hints)} 个。")
    return hints


def validate_tech_params(speaker_count, language_hints):
    if not (SPEAKER_COUNT_MIN <= speaker_count <= SPEAKER_COUNT_MAX):
        die(f"错误：--speaker-count 取值范围 [{SPEAKER_COUNT_MIN}, {SPEAKER_COUNT_MAX}]。")
    if not isinstance(language_hints, list) or not language_hints:
        die("错误：--language-hints 必须至少提供一个语言代码。")


def print_cost(duration, price):
    if duration is not None and price is not None:
        print(f"语音内容时长：{duration:.2f} 秒 | 预估费用：¥{price:.4f}"
              f"（{PRICE_PER_SECOND} 元/秒，最终以百炼账单为准）")
    else:
        print(f"费用按 {PRICE_PER_SECOND} 元/秒计费（按识别出的语音内容时长），"
              "最终以百炼账单为准")


# ============================================================
# RESTful 异步转写（提交 -> 轮询 -> 下载结果）
# ============================================================

def dashscope_base(ws):
    return f"https://{ws}.cn-beijing.maas.aliyuncs.com/api/v1"


def auth_headers(key):
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def submit_task(key, ws, audio_ref, speaker_count, language_hints):
    url = f"{dashscope_base(ws)}/services/audio/asr/transcription"
    payload = {
        "model": MODEL,
        "input": {"file_urls": [audio_ref]},
        "parameters": {
            "channel_id": CHANNEL_ID,
            "diarization_enabled": True,
            "speaker_count": speaker_count,
            "language_hints": language_hints,
        },
    }
    headers = auth_headers(key)
    headers["X-DashScope-Async"] = "enable"
    # 官方说明：RESTful API 使用 oss:// 前缀临时 URL 时，需要该头让服务端解析。
    if str(audio_ref).startswith("oss://"):
        headers["X-DashScope-OssResourceResolve"] = "enable"
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
    except requests.RequestException as exc:
        raise TranscriptionApiError(f"提交转写任务网络错误：{exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    output = data.get("output") or {}
    if resp.status_code != HTTPStatus.OK or not output.get("task_id"):
        raise TranscriptionApiError(
            f"任务提交失败（HTTP {resp.status_code}）：{data.get('message') or data}"
        )
    return output["task_id"], data.get("request_id")


def query_task(key, ws, task_id):
    url = f"{dashscope_base(ws)}/tasks/{task_id}"
    try:
        resp = requests.get(url, headers=auth_headers(key), timeout=30)
    except requests.RequestException as exc:
        raise TranscriptionApiError(f"查询任务网络错误：{exc}") from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code != HTTPStatus.OK:
        raise TranscriptionApiError(
            f"查询任务失败（HTTP {resp.status_code}）：{data.get('message') or data}"
        )
    return data


def wait_task(key, ws, task_id):
    """轮询任务状态，2 秒一次（官方建议 2~5 秒），直到终态。"""
    deadline = time.time() + TASK_WAIT_TIMEOUT_SECONDS
    while True:
        data = query_task(key, ws, task_id)
        status = (data.get("output") or {}).get("task_status")
        if status in ("SUCCEEDED", "FAILED", "UNKNOWN"):
            return data
        if time.time() >= deadline:
            raise TranscriptionApiError(
                "等待转写任务超时；任务仍在运行时可稍后使用同一令牌重试 generate"
            )
        time.sleep(TASK_POLL_INTERVAL_SECONDS)


def upload_local_to_oss(audio, key, ws):
    """把本地音频上传到百炼临时 OSS，返回 oss:// 临时 URL（RESTful API 支持）。"""
    import dashscope
    from dashscope.utils.oss_utils import OssUtils

    dashscope.api_key = key
    dashscope.base_http_api_url = dashscope_base(ws)
    file_url, _ = OssUtils.upload(model=MODEL, file_path=audio, api_key=key)
    return file_url


def download_result_json(transcription_url):
    resp = requests.get(transcription_url, timeout=120)
    resp.raise_for_status()
    return resp.json()


# ============================================================
# 阶段一：prepare（登记确认方案、生成令牌A；绝不调 API）
# ============================================================

def cmd_prepare(args):
    request = read_value(args.request)
    optimized_plan = read_value(args.optimized_plan)
    change_explanation = read_value(args.change_explanation)
    audio = normalize_audio(args.audio)
    validate_plan_fields(request, optimized_plan, change_explanation)
    language_hints = parse_language_hints(args.language_hints)
    validate_tech_params(args.speaker_count, language_hints)

    duration, _ = detect_duration(audio)
    price = estimate_price(duration)
    output = args.output if args.output is not None else default_output()

    spec = canonical_spec(
        request, optimized_plan, change_explanation, audio,
        args.speaker_count, language_hints, output,
    )
    fingerprint = compute_fingerprint(spec)
    token = make_token(fingerprint)

    cleanup_expired()
    save_record(token, {
        "stage": "prepare",
        "fingerprint": fingerprint,
        "request": request,
        "optimized_plan": optimized_plan,
        "change_explanation": change_explanation,
        "audio": audio,
        "params": {
            "speaker_count": args.speaker_count,
            "language_hints": language_hints,
            "output": output,
        },
        "duration_seconds": duration,
        "price_estimate": price,
    })

    print("=== 待确认转写方案（阶段一 prepare，未调用任何 API）===")
    print(f"【原始需求】\n{request}")
    print(f"\n【审核优化后的转写方案】\n{optimized_plan}")
    print(f"\n【改动说明】\n{change_explanation}")
    if is_http_url(audio):
        audio_kind = "公网 URL"
    elif is_oss_url(audio):
        audio_kind = "OSS 临时 URL（RESTful 直传）"
    else:
        audio_kind = "本地文件（generate 阶段自动上传百炼临时 OSS）"
    print(f"\n【音频】{audio}（{audio_kind}）")
    print(f"【参数】模型：{MODEL} | 说话人数：{args.speaker_count}（diarization_enabled） | "
          f"语言提示：{', '.join(language_hints)} | 音轨：{CHANNEL_ID}")
    print(f"【输出基础名】{output}（将生成 .txt 与 .json）")
    print_cost(duration, price)
    print("\n=== 确认令牌（Token A）===")
    print(token)
    print("\n请将以上方案原样回显给用户确认；用户确认后用此令牌 + 同一音频与优化后方案调用 generate 阶段。")
    # 注意：本函数绝不调用真实 API、绝不读取密钥 -- 这是「绝不调 API」的结构保证。


# ============================================================
# 阶段二：generate（三道检查通过后才提交转写；成功后销毁令牌）
# ============================================================

def cmd_generate(args):
    token = args.token

    # 检查1：待确认记录是否存在
    rec = load_record(token)
    if rec is None:
        die("错误：无待确认记录或令牌A已失效/已使用。\n"
            "必须先完成 prepare（用户确认）环节拿到确认令牌(Token A)，才能进入 generate 环节。\n"
            "正确流程：prepare -> 用户确认 -> generate。")

    # 检查2：令牌是否匹配
    if rec.get("token") != token:
        die("错误：确认令牌(Token A)不匹配。")

    # 检查3：提交的音频/优化后方案与确认版本是否一致（指纹 + 明文双保险，防篡改）
    audio = normalize_audio(args.audio)
    optimized_plan = read_value(args.optimized_plan)
    params = dict(rec.get("params") or {})
    spec_check = canonical_spec(
        rec.get("request", ""), optimized_plan, rec.get("change_explanation", ""),
        audio, params.get("speaker_count"), params.get("language_hints"), params.get("output"),
    )
    if (compute_fingerprint(spec_check) != rec.get("fingerprint")
            or audio != rec.get("audio")
            or optimized_plan != rec.get("optimized_plan")):
        die("错误：提交的音频或优化后方案与 prepare 确认版本不一致（防「展示一套、转写另一套」）。\n"
            "请用 prepare 确认的音频与优化后方案调用 generate，或重新 prepare 后再 generate。")

    # 三道检查全过 -- 从记录回取并锁定参数（-o 可覆盖输出基础名）
    locked = dict(params)
    if args.output is not None:
        locked["output"] = args.output
    out_base = locked["output"]
    duration = rec.get("duration_seconds")
    if duration is None and not is_remote_audio(audio):
        duration, _ = detect_duration(audio)
    price = estimate_price(duration)

    print(f"模型：{MODEL} | 说话人数：{locked['speaker_count']}（diarization_enabled） | "
          f"语言提示：{', '.join(locked['language_hints'])} | 音轨：{CHANNEL_ID}")
    print(f"音频：{audio}")
    print_cost(duration, price)

    # 以下才允许读取密钥与调用网络 API；prepare 路径永远到不了这里。
    key = need_key()
    ws = need_workspace()

    try:
        if is_remote_audio(audio):
            audio_ref = audio
        else:
            print("正在上传本地音频到百炼临时 OSS ...")
            audio_ref = upload_local_to_oss(audio, key, ws)
            print(f"上传完成：{audio_ref}")

        task_id, submit_request_id = submit_task(
            key, ws, audio_ref, locked["speaker_count"], locked["language_hints"]
        )
        print(f"任务已提交，task_id：{task_id}")

        task_data = wait_task(key, ws, task_id)
        output = task_data.get("output") or {}
        task_status = output.get("task_status")
        print(f"任务状态：{task_status}")
        if task_status != "SUCCEEDED":
            die(f"错误：转写任务未成功（{task_status}）：{output.get('message') or output}\n"
                "令牌A保留，可稍后使用同一令牌重试 generate（无需重新 prepare）。")

        results = output.get("results") or []
        succeeded = [
            r for r in results
            if (r.get("subtask_status") or "").upper() == "SUCCEEDED" and r.get("transcription_url")
        ]
        if not succeeded:
            die("错误：任务成功但未返回可下载的识别结果（subtask_status 非 SUCCEEDED）。\n"
                "令牌A保留，可稍后使用同一令牌重试 generate。")
        transcription_url = succeeded[0]["transcription_url"]

        # 任务成功（计费已发生）后立即销毁令牌，防止重复提交。
        destroy_record(token)
        print("令牌A已销毁（用完即焚），不可复用。")

        result_doc = download_result_json(transcription_url)
        sentences, result_duration = parse_result_document(result_doc)
        if result_duration is not None:
            duration = result_duration
            price = estimate_price(duration)
    except TranscriptionApiError as exc:
        die(f"错误：{exc}\n令牌A保留，可稍后使用同一令牌重试 generate（无需重新 prepare）。")
    except Exception as exc:  # noqa: BLE001 - 统一转为用户可读错误
        hint = ""
        if "transcription_url" in locals() and transcription_url:
            hint = f"\n任务已成功，可直接下载结果：{transcription_url}"
        else:
            hint = "\n令牌A保留，可稍后使用同一令牌重试 generate。"
        die(f"错误：语音识别调用失败：{exc}{hint}")

    if not sentences:
        print("警告：识别成功但未返回有效句子（可能无语音或语音过短）。")
    meta = {
        "model": MODEL,
        "audio": audio,
        "audio_ref": audio_ref,
        "task_id": task_id,
        "request_id": task_data.get("request_id") or submit_request_id,
        "speaker_count": locked["speaker_count"],
        "language_hints": locked["language_hints"],
        "channel_id": CHANNEL_ID,
        "diarization_enabled": True,
        "duration_seconds": duration,
        "price_estimate": price,
        "price_per_second": PRICE_PER_SECOND,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    txt_path, json_path = write_outputs(out_base, meta, sentences)

    print(f"requestId：{meta['request_id']}")
    print(f"task_id：{task_id}")
    print(f"句子数：{len(sentences)}")
    print_cost(duration, price)
    print(f"转写结果(TXT)：{txt_path}")
    print(f"结构化结果(JSON)：{json_path}")


# ============================================================
# 入口：子命令派发（无直接生成路径，裸调用直接报错）
# ============================================================

def main():
    ensure_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="调用阿里云百炼 Qwen-Audio-3.0-ASR-Flash-Filetrans 录音文件异步转写"
                    "（两阶段：prepare 登记确认方案 / generate 经确认后提交任务）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "两阶段工作流（代码兜底，转写前必须确认方案）：\n"
            "  1) prepare：登记审核优化后的转写方案并生成令牌A，绝不调 API。\n"
            "  2) 将方案回显用户确认。\n"
            "  3) generate：用令牌A + 同一音频/优化后方案提交异步转写任务"
            "（过三道检查后才调真实 API，成功后立即销毁令牌）。\n\n"
            "音频输入：公网 HTTP/HTTPS URL，或本地文件（generate 阶段自动上传百炼临时 OSS）。\n\n"
            "示例：\n"
            "  prepare：  python scripts/asr_qwen.py prepare --request \"转写会议录音并区分说话人\" "
            "--optimized-plan \"单文件自动说话人分离，输出带时间戳的 TXT+JSON\" "
            "--change-explanation \"按需求启用 diarization_enabled，锁定 speaker_count=2，"
            "输出 TXT+JSON\" --audio https://example.com/meeting.wav\n"
            "  generate： python scripts/asr_qwen.py generate --token <令牌A> "
            "--audio https://example.com/meeting.wav "
            "--optimized-plan \"单文件自动说话人分离，输出带时间戳的 TXT+JSON\"\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pp = sub.add_parser("prepare", help="登记待确认转写方案并返回令牌A（不调 API）")
    pp.add_argument("--request", required=True, help="原始需求（可用 @文件）")
    pp.add_argument("--optimized-plan", dest="optimized_plan", required=True,
                    help="审核优化后的转写方案说明（可用 @文件）")
    pp.add_argument("--change-explanation", dest="change_explanation", required=True,
                    help="改动说明，≥20 字防敷衍（可用 @文件）")
    pp.add_argument("--audio", required=True,
                    help="待转写音频：公网 HTTP/HTTPS URL，或本地文件路径")
    pp.add_argument("--speaker-count", dest="speaker_count", type=int, default=2,
                    help="说话人数 [2,100]，默认 2（自动分离）")
    pp.add_argument("--language-hints", dest="language_hints", default="zh",
                    help="语言提示，逗号分隔，最多 4 个，默认 zh")
    pp.add_argument("-o", "--output", default=None,
                    help="输出基础名（无扩展名，默认 output/asr_<时间戳>）")
    pp.set_defaults(func=cmd_prepare)

    pg = sub.add_parser("generate", help="用令牌A+确认版本提交转写（三道检查后才调真实 API）")
    pg.add_argument("--token", required=True, help="prepare 返回的确认令牌(Token A)")
    pg.add_argument("--audio", required=True,
                    help="与 prepare 确认一致的公网 URL 或本地文件路径")
    pg.add_argument("--optimized-plan", dest="optimized_plan", required=True,
                    help="与 prepare 确认一致的优化后方案说明（可用 @文件）")
    pg.add_argument("-o", "--output", default=None, help="覆盖输出基础名（可选）")
    pg.set_defaults(func=cmd_generate)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
