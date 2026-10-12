# Qwen-Audio-3.0-ASR-Flash-Filetrans API 参考

## 模型与端点

- 模型 ID：`qwen-audio-3.0-asr-flash-filetrans`
- 地域：华北2（北京），业务空间专属域名
  - 提交任务：`POST https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/audio/asr/transcription`
  - 查询任务：`GET https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/tasks/{task_id}`
- WorkspaceId：通过环境变量 `DASHSCOPE_WORKSPACE_ID` 配置；本技能包不内置任何业务空间 ID。
- 鉴权：`DASHSCOPE_API_KEY` 环境变量，请求头 `Authorization: Bearer <key>`。
- 提交请求必须带 `X-DashScope-Async: enable`，并始终携带 `parameters` 对象（无参数也传 `{}`）。
- 使用 `oss://` 前缀临时 URL 提交时，必须额外带请求头 `X-DashScope-OssResourceResolve: enable`（本技能本地文件自动上传时已自动加该头）。

## 音频输入

- `input.file_urls`：数组，单次仅支持 1 个 URL，支持 HTTP/HTTPS 公网 URL。
- 本技能也接受 `oss://` 前缀临时 URL 直接提交（RESTful API 支持，脚本会自动带解析头）。
- 本地文件**不支持直接识别**；本技能在 generate 阶段先上传到百炼临时 OSS，得到 `oss://` 前缀临时 URL 后用 RESTful API 提交（SDK 的 `Transcription` 不支持 `oss://`，因此脚本统一走 RESTful 流程）。
- 限制：单文件不超过 2GB、12 小时；任意采样率，兼容 wav/mp3/aac 等主流音视频格式。
- 生产环境建议使用阿里云 OSS 等稳定存储；临时 URL/上传凭证接口不适合高并发生产场景。

## 本技能使用的参数

提交请求体：

```json
{
  "model": "qwen-audio-3.0-asr-flash-filetrans",
  "input": {
    "file_urls": ["https://example.com/meeting.wav"]
  },
  "parameters": {
    "channel_id": [0],
    "diarization_enabled": true,
    "speaker_count": 2,
    "language_hints": ["zh"]
  }
}
```

| 参数 | 取值 | 说明 |
|------|------|------|
| `channel_id` | `[0]` | 音轨索引，从 0 开始；多音轨会独立计费 |
| `diarization_enabled` | `true` | 自动说话人分离（本技能固定开启；仅单声道，建议 ≤2 小时） |
| `speaker_count` | `2-100` | 期望说话人数，仅分离开启时生效 |
| `language_hints` | `zh/en/ja/ko/vi/th/id/ms` 等 | 语种提示，最多 4 个 |

官方可选能力（v2 未启用）：预编译热词 `vocabulary_id`、即时热词 `vocabulary`（仅本模型支持）、敏感词过滤 `special_word_filter`、上下文增强、EventBridge 回调。

## 任务流程

1. `POST /services/audio/asr/transcription` 提交任务，返回 `output.task_id`。
2. 每 2 秒轮询 `GET /tasks/{task_id}`，状态包括 `PENDING`、`RUNNING`、`SUCCEEDED`、`FAILED`。
3. 任务成功后 `output.results[].subtask_status == "SUCCEEDED"`，通过 `output.results[].transcription_url` 下载完整结果 JSON。
4. `transcription_url` 24 小时内有效，脚本下载后落盘保存。

轮询查询接口默认 20 QPS、最高可扩容至 100 QPS；高并发建议配置 EventBridge 回调替代轮询。

## 识别结果 JSON（transcription_url 下载内容）

```json
{
  "file_url": "https://example.com/meeting.wav",
  "properties": {
    "audio_format": "pcm_s16le",
    "channels": [0],
    "original_sampling_rate": 16000,
    "original_duration_in_milliseconds": 2600
  },
  "transcripts": [
    {
      "channel_id": 0,
      "content_duration_in_milliseconds": 2480,
      "text": "大家好，今天汇报开始。",
      "sentences": [
        {
          "begin_time": 0,
          "end_time": 1200,
          "text": "大家好，今天汇报开始。",
          "speaker_id": 0,
          "words": [
            {"begin_time": 0, "end_time": 400, "text": "大家好", "punctuation": "，"}
          ]
        }
      ]
    }
  ]
}
```

关键字段：

| 字段 | 说明 |
|------|------|
| `transcripts[].sentences[]` | 句级结果 |
| `sentences[].begin_time/end_time` | 音频内时间戳（毫秒） |
| `sentences[].words[]` | 字/词级时间戳 |
| `sentences[].speaker_id` | 说话人索引（0 起始，仅在开启分离时返回） |
| `transcripts[].content_duration_in_milliseconds` | 被判定为语音内容的时长，用于计费 |

时间戳单位均为毫秒，与任务级 `end_time`（字符串日期）不是同一字段。

## Python SDK 示例（公网 URL，官方快速开始）

```python
from http import HTTPStatus
from dashscope.audio.asr import Transcription
import dashscope
import os

dashscope.base_http_api_url = 'https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1'
dashscope.api_key = os.getenv("DASHSCOPE_API_KEY")

task_response = Transcription.async_call(
    model='qwen-audio-3.0-asr-flash-filetrans',
    file_urls=['https://example.com/meeting.wav'],
    language_hints=['zh'],
)
transcription_response = Transcription.wait(task=task_response.output.task_id)
if transcription_response.status_code == HTTPStatus.OK:
    for transcription in transcription_response.output['results']:
        if transcription['subtask_status'] == 'SUCCEEDED':
            print(transcription['transcription_url'])
```

本技能脚本为了让本地文件也能用，改为 RESTful 提交 + 轮询 + 下载结果；公网 URL 走同样的 RESTful 路径，行为与 SDK 一致。

## 注意

- 完整官方文档可在阿里云百炼「非实时语音识别」文档中查看；字段名以真实调用返回为准，脚本已做缺失回退与 0 起始说话人映射。
- 失败重试：技能在任务未成功计费时保留令牌，可在不重复 prepare 的情况下重试 generate。
