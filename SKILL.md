---
name: skill-asr-qwen
description: "基于阿里云百炼 DashScope 的 Qwen-Audio-3.0-ASR-Flash-Filetrans 录音文件异步转写技能。核心升级：转写前必须让用户确认方案，确认逻辑写进工具脚本本身（代码兜底，唯一 100% 有效）--拆成 prepare（审核优化转写方案、落地待确认记录、返回令牌A，绝不调 API）与 generate（三道检查通过后才提交真实转写任务、成功后立即销毁令牌A）两阶段。支持公网 HTTP/HTTPS 音频 URL 与本地音频文件（generate 阶段自动上传百炼临时 OSS），单文件自动说话人分离（diarization_enabled + speaker_count），输出带说话人与时间戳的 TXT 与结构化 JSON，0.00022 元/秒。当用户想把语音/音频/会议录音/汇报音频转成文字、区分说话人、生成会议转写文本时使用。Use when the user wants non-realtime speech-to-text transcription from an audio file or public audio URL, speaker diarization, or meeting/report transcription via Qwen-Audio-3.0-ASR-Flash-Filetrans."
---

# skill-asr-qwen 技能

用阿里云百炼 DashScope 的 **Qwen-Audio-3.0-ASR-Flash-Filetrans**（模型 ID：`qwen-audio-3.0-asr-flash-filetrans`）做**录音文件异步转写**，输出带说话人与时间戳的文字。支持公网 HTTP/HTTPS 音频 URL，也支持本地文件（generate 阶段自动上传百炼临时 OSS）；按识别出的语音内容时长计费（华北2北京 **0.00022 元/秒**，国际 0.00026 元/秒）。

## 核心升级：两段式令牌链条（代码兜底，唯一 100% 有效）

转写是真实计费 API 调用，**生成前必须让用户确认方案**。确认逻辑写进脚本本身，拆成两阶段：

- **阶段一 `prepare`**：模型按整体开发需求完成审核与优化，登记「原始需求 -> 优化后方案 -> 改动说明」（改动说明 ≥20 字防敷衍），锁定音频与转写参数，生成**令牌A**（绑定方案内容指纹 `sha256`），落盘待确认记录。**绝不调用真实 API、绝不读取密钥、不耗费用**。
- **阶段二 `generate`**：调真实 API 前先过**三道检查**，任一不通过直接拒绝：
  1. **记录是否存在**：无对应令牌A的待确认记录（或已销毁/过期）-> 拒绝。
  2. **令牌是否匹配**：记录内令牌与提交令牌不一致 -> 拒绝。
  3. **内容是否一致**：提交的音频与优化后方案与确认版本不一致（指纹 + 明文双保险）-> 拒绝。
  - 全过才提交异步转写任务（`POST /api/v1/services/audio/asr/transcription`），轮询 `GET /api/v1/tasks/{task_id}`，任务成功后下载 `transcription_url` 结果并**立即销毁令牌A**（计费已发生，防重复提交）；任务失败则保留令牌允许重试。

> **代码兜底保证**：即使用户未确认、或技能"忘了"等待确认，`generate` 缺有效 `prepare` 记录会被脚本直接拒绝，真实 API 那行代码不可达。这不是"建议确认"，而是"无法绕过"。
>
> **令牌是流程副产品，非写死密码**：由代码动态生成内容指纹，模型读源码也无法伪造；令牌与内容绑定，展示一套、转写另一套会被拦下；报错文案即流程指南。

## 工作流（必须遵守，四步）

**①需求澄清 -> ②prepare 回显方案 -> ③用户确认 -> ④generate 转写与报告**。

### 第一步：需求澄清
1. 确认**音频来源**：公网 HTTP/HTTPS URL，或本地文件路径（单文件）。
2. 确认**说话人数**（默认 2，范围 2-100；单文件自动分离，输出 说话人1/2/…）。
3. 确认**语言提示**（默认 zh，最多 4 个，如 `zh,en`）、**输出基础名**（默认 `output/asr_<时间戳>`）。
4. 评估音频质量：单声道、无回声、信噪比高时效果最好；URL 不可访问、本地文件不存在/为空时 prepare 直接拒绝。

### 第二步：prepare 审核优化并登记（绝不调 API）
调用 `prepare`，登记「原始需求 -> 优化后方案 -> 改动说明」并锁定参数：

```powershell
& python scripts/asr_qwen.py prepare `
  --request "转写会议录音并区分说话人" `
  --optimized-plan "单文件自动说话人分离：开启 diarization_enabled，speaker_count=2，输出带时间戳的 TXT+JSON" `
  --change-explanation "按需求启用自动说话人分离并锁定 2 人；语言提示用 zh 提升中文准确率；输出 TXT+JSON 兼顾可读与结构化。" `
  --audio meeting.wav --speaker-count 2 --language-hints zh
```

- `prepare` 必需参数：`--request`（原始需求）、`--optimized-plan`（审核优化后的方案）、`--change-explanation`（改动说明，**≥20 字**防敷衍）、`--audio`（公网 URL 或本地路径）。
- 技术参数（speaker_count/language_hints/output）在 prepare 时**锁定进记录**，generate 阶段不可篡改。
- 长文本可用 `@文件路径` 传入，规避命令行长度限制。
- `prepare` 输出：原始需求、优化后方案、改动说明、音频与参数、预估费用、**确认令牌(Token A)**。绝不调 API、不耗费用。

### 第三步：回显确认（强制等待用户确认）
把 `prepare` 返回的**方案 + 参数 + 预估费用 + 令牌A**完整回显给用户，并**等待用户明确确认**：

- 用户确认 -> 进入第四步，用令牌A + **同一音频** + **同一优化后方案**调用 `generate`。
- 用户要求修改 -> 调整方案/参数后**重新 `prepare`**（产生新令牌，旧令牌自然失效/过期清理），再次回显确认。
- ⚠️ **不得在用户确认前调用 `generate`**。代码兜底虽不验证"用户是否真的看过"，但 `generate` 必须依赖一个有效的 `prepare` 记录；规范的回显确认是质量与成本的关键闸门。

### 第四步：generate 转写与报告（三道检查 -> 提交任务 -> 销毁令牌 -> 输出）
用户确认后，用令牌A + 与确认版本完全一致的音频/优化后方案调用 `generate`：

```powershell
& python scripts/asr_qwen.py generate `
  --token <令牌A> `
  --audio meeting.wav `
  --optimized-plan "单文件自动说话人分离：开启 diarization_enabled，speaker_count=2，输出带时间戳的 TXT+JSON" `
  [-o output/meeting]
```

- 脚本过三道检查（记录存在 / 令牌匹配 / 音频与方案一致）后才提交真实任务；其余参数从记录回取并锁定，**不可在 generate 阶段篡改**；`-o` 可覆盖输出基础名。
- 本地文件会在 generate 阶段自动上传百炼临时 OSS；公网 URL 直接提交。任务成功（计费已发生）后**立即销毁令牌A**（一次性，防重复计费），随后下载并写出 `TXT`（`[mm:ss.mmm - mm:ss.mmm] 说话人N：文本`）与 `JSON`（元数据 + 逐句 speaker/time/text/confidence/words）。
- 报告 requestId、task_id、句子数、语音内容时长与费用估算；最终计费以百炼账单为准。
- **失败恢复**：任务提交/轮询/识别失败且未成功计费时令牌A保留，可稍后同令牌重试；任务已成功后如需再次转写必须重新 prepare -> 确认 -> generate。
- **迭代**：效果不理想优先调整说话人数、语言提示、音频清晰度或声道，每次改动后须重新 prepare 确认再 generate。

## 参数与默认值

| 参数 | 默认 | 说明 |
|------|------|------|
| `--request` | 必填 | 原始需求，支持 `@文件` |
| `--optimized-plan` | 必填 | 审核优化后的转写方案说明，支持 `@文件` |
| `--change-explanation` | 必填 | 改动说明，≥20 字防敷衍，支持 `@文件` |
| `--audio` | 必填 | 公网 HTTP/HTTPS 音频 URL、`oss://` 临时 URL，或本地文件路径（prepare/generate 须完全一致） |
| `--speaker-count` | `2` | 说话人数 [2,100]，配合 `diarization_enabled` 自动分离 |
| `--language-hints` | `zh` | 语言提示，逗号分隔，最多 4 个 |
| `-o/--output` | `output/asr_<时间戳>` | 输出基础名（无扩展名，生成 `.txt` 与 `.json`） |

> 上述参数在 `prepare` 时锁定进记录；`generate` 仅可用 `-o` 覆盖输出基础名。
> filetrans 支持任意采样率与 wav/mp3/aac 等主流音视频格式，因此技能不再要求 format/sample_rate。

## 官方限制与最佳实践

- 单文件不超过 **2GB、12 小时**；开启说话人分离时建议音频不超过 **2 小时**，且仅支持**单声道**。
- `file_urls` 单次仅支持 1 个 URL，支持 HTTP/HTTPS；SDK 方式不支持 `oss://` 前缀，本技能统一走 RESTful API，本地文件先上传到百炼临时 OSS 再提交（`oss://` 临时 URL 需带 `X-DashScope-OssResourceResolve: enable` 请求头，脚本已自动处理）。
- 任务完成后 `transcription_url` 仅 **24 小时**有效，脚本会立即下载落盘。
- 生产环境建议把音频放入阿里云 OSS 并保证公网可访问，或配置 EventBridge 回调替代高频轮询；本技能单任务轮询间隔 2 秒。
- 未传 `special_word_filter` 时服务端默认启用系统敏感词过滤（替换为等长 `*`），如需关闭/自定义可参考官方 API 参考。

## 环境准备

- 必须设置环境变量 `DASHSCOPE_API_KEY`（阿里云百炼 API Key）。
- PowerShell 临时：`$env:DASHSCOPE_API_KEY = '<你的APIKey>'`
- 持久：`setx DASHSCOPE_API_KEY '<你的APIKey>'` 后重开终端。
- 必须通过环境变量设置 `DASHSCOPE_WORKSPACE_ID`；本技能包不内置 Workspace ID。
- 运行解释器：任意 Python 3.10+ 环境；需要 `requests` 与 `dashscope>=1.26.0`。
- API Key 获取：https://help.aliyun.com/zh/model-studio/get-api-key

## 重要约束

- **密钥安全**：只从环境变量 `DASHSCOPE_API_KEY` 读取，绝不写入技能文件，绝不在聊天中索要明文密钥。
- **两阶段不可绕过**：无 `prepare` 记录则 `generate` 被拒；`prepare` 不调 API、不耗费用。
- **回显确认强制**：`prepare` 后必须将方案与令牌回显用户确认，确认前不得 `generate`。
- **防敷衍强制**：`prepare` 必须登记「原始需求 -> 优化后方案 -> 改动说明」（≥20 字），供用户对比确认。
- **令牌一次性**：`generate` 任务成功后立即销毁令牌A；任务失败且未计费则令牌A保留重试。
- **单文件自动分离**：v2 只做单文件 `diarization_enabled + speaker_count`；不做多文件逐说话人、不做 SRT、不做实时麦克风、不接热词/上下文增强。
- **说话人字段兼容**：结果优先读 `sentence.speaker_id`（0 起始），输出统一转成 `说话人1/2/…`；缺失时按顺序回退。
- **输出位置**：默认存当前工作目录 `output/` 下；生成后如需移入项目，自行移动。

## 参考文件索引

| 文件 | 内容 |
|------|------|
| `references/api_reference.md` | 模型 ID、RESTful/SDK 调用、参数、任务轮询、结果字段 |
| `references/pricing.md` | 0.00022/0.00026 元/秒计费与免费额度说明 |
| `scripts/asr_qwen.py` | 两阶段转写脚本（prepare/generate，代码兜底两段式令牌门禁） |
| `tests/test_asr_qwen.py` | 无 API 单元测试：prepare/generate 门禁、异步解析、输出、令牌生命周期 |
