---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '51ab1684-d372-4162-8e6b-75a951397d45'
  PropagateID: '51ab1684-d372-4162-8e6b-75a951397d45'
  ReservedCode1: 'ecbf8475-9e66-4534-9a7e-1e5b5989649b'
  ReservedCode2: 'ecbf8475-9e66-4534-9a7e-1e5b5989649b'
---

# AiMeeting 开发交接文档

> 最后更新：2026-09-11 ｜ 仓库：`https://github.com/KevinLiss/ApeAdmin-AiMeeting.git`（main 分支）
> 本文档面向接手开发的同学，覆盖架构、数据流、环境、运维与已知问题。

---

## 1. 项目定位

ApeAdmin 的「AI 会议」插件（类讯飞听见）：会议参与者用手机/电脑打开 H5 →
输入会议编号进入会议 → 点「正式开始会议」持续录音 → 语音近实时转写为文字流
（气泡式聊天流）→ 声纹自动区分说话人（显示为「发言者A001」，可改名）→
结束存档，管理端可查看全部会议、转写记录、说话人与 AI 纪要。

三个组成部分：

| 部分 | 目录 | 说明 |
|---|---|---|
| 后端插件 | `backend/src/plugins/builtin/aimeeting/` | FastAPI 插件，需拷贝回 ApeAdmin 主项目运行 |
| 管理端页面 | `frontend/src/views/aimeeting/` + `frontend/src/api/aimeeting.ts` | 拷贝回 ApeAdmin 前端 |
| 用户端 H5 | `frontend-h5/` | 独立 Vue3+Vite 工程，移动优先 |

## 2. 核心数据流（务必先看懂这条链路）

```
H5 Meeting.vue
  └─ useRecorder（MediaRecorder 滚动重启，15s 一片）
       │  每 15s：stop → 上传完整 WebM（offset_sec=已录秒数）→ start 新 recorder
       ▼
POST /client/meetings/{id}/audio   （登记 processing 记录，立即返回）
       │
       ▼ asyncio.create_task(transcribe_record_async)
faster-whisper 转写（线程池，句级时间戳 = offset + 段内时间）
       │
       ▼ merge_transcript_to_meeting
aimeeting_meetings.transcript_json = [{start,end,text,speaker}]（绝对时间轴）
       │
       ▼ 会议 in_progress 且模型存在
run_diarization(incremental=True)（sherpa-onnx 声纹聚类）
       │  拼接 m{id}_merged.wav（numpy 按 offset 补静音）→ 聚类 → 时间重叠对齐
       ▼ 回填 speaker → aimeeting_speakers 表（发言者A001）
H5 每 5s 轮询 GET /client/meetings/{id}/transcript → 气泡流渲染
       │
       ▼ 用户点「结束会议」
POST /finish（立即返回）→ 后台 finalize_meeting：
  等在途转写（≤90s）→ 合并 → 全量说话人分离 → AI 纪要（LLM）
```

**关键设计决策（为什么这么做）**：

1. **MediaRecorder 滚动重启而非 timeslice**：Chrome 的
   `start(timeslice)` 只有第一个 blob 含 WebM 头，后续是裸 Opus 无法解码
   （ffmpeg 报 Invalid data）。滚动重启每片都是完整 WebM。
2. **绝对时间轴**：句级时间戳用「会议内绝对秒数」（offset+段内），说话人
   分离按同一时间轴对齐；合并音频用 numpy 按 offset 放置、空隙补静音，
   而非顺序拼接，避免时间漂移。
3. **增量声纹分离依赖 `status == "in_progress"`**：前端必须先调
   `/start` 再录音，否则增量分离永不触发（曾踩坑修复）。
4. **merge 保留已回填的 speaker**：`merge_transcript_to_meeting` 按
   (start,end) 时间段键保留旧 speaker，避免每次新片合并把分离结果清零。
5. **finish 立即返回**：转写/分离/纪要在后台 `finalize_meeting` 按序执行，
   不阻塞接口响应（曾阻塞 30s，已改）。
6. **设备绑定**：会议绑定首个访问设备（device_id），其他设备 403。这是
   无登录态场景的轻量凭证。

## 3. 数据库（4 张表，SQLite：`apeadmin/backend/apeadmin.db`）

- `aimeeting_meetings`：会议主表。重点字段：`meeting_code`（唯一入口凭证）、
  `device_id`（绑定设备）、`status`（scheduled/in_progress/ended/cancelled）、
  `transcript_text`（完整文本，带「发言者A001: 」前缀）、`transcript_json`
  （句级数组）、`diarization_status`、`audio_file`（合并 wav 路径）。
- `aimeeting_records`：每片录音一条。`offset_sec`（会议内偏移）、
  `segments_json`（句级）、`transcript_status`。
- `aimeeting_speakers`：声纹聚类的虚拟说话人。`speaker_no`（聚类编号 1 起）、
  `display_name`（默认「发言者A001」，可改）。
- `aimeeting_minutes`：AI 纪要。`summary`（一句话）+ `minutes`（Markdown 结构化）。

编号规则 `speaker_code(no)`：1→A001，27→A002…（letter=letters[(no-1)%26]，
seq=(no-1)//26+1）。**注意编号跨增量轮次可能漂移**（聚类不保证稳定，见 bug 清单）。

## 4. 后端接口一览

**用户端（会议编号+设备标识，无登录）**：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/client/meetings/lookup` | 编号查会议并绑定设备 |
| POST | `/client/meetings` | 创建会议（名称可选自动命名） |
| PATCH | `/client/meetings/{id}` | 改名 |
| POST | `/client/meetings/{id}/start` | scheduled→in_progress（增量分离依赖） |
| POST | `/client/meetings/{id}/audio` | 上传录音片（≤50MB，登记后台转写） |
| POST | `/client/meetings/{id}/finish` | 立即返回，后台 finalize |
| GET | `/client/meetings/{id}` | 详情（含 transcript_json/minutes/speakers） |
| GET | `/client/meetings/{id}/transcript` | 轻量轮询：segments+状态+说话人名 |
| PATCH | `/client/meetings/{id}/speakers/{sid}` | 说话人改名 |

**管理端（登录 + `aimeeting:` 权限）**：`/meetings` CRUD、`/records`、
`/minutes`、`/minutes/generate`。另有 2 个 MCP 工具（`aimeeting_list_meetings`、
`aimeeting_list_records`）。

## 5. 环境、启动与运维

**端口分配**：后端 8000（本插件所在 ApeAdmin）、H5 dev 5177、8100 是别的项目勿动。

**后端启动（macOS 必须绕过系统代理）**：

```bash
cd apeadmin/backend
NO_PROXY='*' nohup .venv/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 8000 > /tmp/apeadmin_backend.log 2>&1 &
```

- 日志含二进制字符，用 `grep -a` 过滤；查错：`grep -a -E "ERROR|Traceback|aimeeting" /tmp/apeadmin_backend.log`
- 重启：`kill $(lsof -ti :8000)` 再启动
- **改动仓库代码后必须同步到运行目录再重启**：
  `cp aimeeting/backend/src/plugins/builtin/aimeeting/*.py apeadmin/backend/src/plugins/builtin/aimeeting/`

**H5 启动**：

```bash
cd aimeeting/frontend-h5
nohup npm run dev > /tmp/aimeeting_h5_vite.log 2>&1 &   # vite 5177，proxy 到 8000
npm run build   # 生产构建（无类型检查，会出 dist/）
```

⚠️ **vite dev server 会无故宕机**（2026-09-11 凌晨发生过一次，页面 JS 还活着
但 API 全挂，表象是"计时器在走但没有转写输出"）。排查口诀：先 `lsof -ti :5177`
确认进程活着，死了就重启 vite。

**模型文件（不进 git，位于 `apeadmin/backend/models/`）**：

- `faster-whisper-small/`：语音转写（CPU int8，懒加载单例）
- `sherpa-diarization/`：说话人分离（segmentation.onnx + embedding.onnx）

环境变量可覆盖：`AIMEETING_AUDIO_DIR`、`AIMEETING_WHISPER_MODEL`、
`AIMEETING_DIARIZATION_MODEL`。

**录音存储**：`apeadmin/backend/storage/aimeeting/audio/`，命名 `m{meeting_id}_{uuid}.webm`，
合并产物 `m{id}_merged.wav`。

**DB 直查**：`sqlite3 apeadmin/backend/apeadmin.db "SELECT ... FROM aimeeting_meetings ..."`

## 6. 前端结构（frontend-h5）

```
src/
├── views/Home.vue          # 编号进入 / 创建会议
├── views/Meeting.vue       # 会议页：控制区 + 转写流 + 说话人列表 + 纪要
├── composables/useRecorder.ts  # 录音：权限三态 + 滚动切片 + 上传队列
└── api/aimeeting.ts        # 接口封装（deviceId 存 localStorage）
```

- 权限三态 UI：granted（绿色就绪）/ prompt（授权按钮）/ denied（红色指引），
  `permissions.query` 无感检测 + change 监听；Safari 不支持 query 降级 unknown。
- 切片上传队列：忙时入队、空闲 flush（避免并发上传互相静默丢片）；
  `waitForUploads()` 供结束会议前等待队列清空。
- 转写流 5s 轮询；`diarization_status` 变化时触发整页 refresh（说话人出现）。
- 头像取 speaker_name 首字母：正则 `/(?:^|[^A-Z])([A-Z])\d{3}/` 匹配
  「发言者A001」提取 A，中文自定义名取首字。

## 7. 部署（生产）

- 后端：`scp` 到服务器 `/www/wwwroot/apeadmin/backend/` + `systemctl restart`
- 前端 H5：`npm run build` 后上传 `dist/`（nginx 托管；`/api` 反代后端）
- CORS：跨域部署需在 `backend/src/core/config.py` 的 `CORS_ORIGINS` 加 H5 域名
- ApeAdmin 后台菜单由插件 seed 写入数据库，删除菜单需同步改 seed.py + 清理数据库

## 8. 开发节奏与近期变更

- 2026-09-10：全链路 E2E 打通（会议16「Q3 产品评审会」）；
  修复 MediaRecorder 后续分片裸 Opus 不可解码（滚动重启方案）、
  `_merge_audio_files` 顺序拼接导致时间轴漂移（numpy 按 offset 补静音）。
- 2026-09-10 晚：录音权限三态 UI；昵称/头像（这是東風OS 项目的事，勿混淆）。
- 2026-09-11 凌晨：实时转写流重构 + 3 个 bug 修复（startMeeting 漏调、
  merge 清零 speaker、resume 不重启 sliceTimer）；说话人格式统一「发言者A001」。
- 2026-09-11 本轮：见第 9 节（本次修复清单）。

详细 bug 清单与功能评审见 `docs/BUGS_AND_REVIEW.md`。

## 9. 本次（2026-09-11）修复清单

1. **前端 useRecorder 切片上传队列化**：原 `if (sliceUploadBusy) return` 静默
   丢弃并发分片（慢网络丢数据）→ 改为 pendingSlices 队列串行 flush；
   durationSec 在分片停止那一刻固化（避免排队期间 elapsed 增长虚增时长，
   令后端合并音频尾部多出静音）。
2. **前端 handleStart 启动顺序**：改为先 `recorder.start()`（验证麦克风可用）
   再调 `startMeeting`；录音失败不再令会议卡 in_progress；start 接口失败则
   回滚停录音。
3. **前端 handleFinish**：结束前 `waitForUploads()` 等队列清空，避免在途分片
   被 finalize 漏掉；`minutes.value` 赋值兼容 data.minutes 结构。
4. **前端 onBeforeUnmount**：离开页面时上传残片（原先直接丢弃 ≤15s 尾巴）。
5. **前端 avatarText 正则**：`/^([A-Z])\d+/` 对「发言者A001」不匹配
   （^ 锚点在「发」处失败）→ 改 `/(?:^|[^A-Z])([A-Z])\d{3}/` 提取 A。
6. **后端 finish 接口去阻塞**：原先 `await wait_for_records_done(timeout=30)`
   把等待放在响应路径上（用户点结束最长卡 30s）→ 移入后台 finalize_meeting
   （其内部本就有 ≤90s 等待），接口立即返回。
7. **后端 `_sanitize_client_meeting` 矛盾**：client_get_meeting 显式塞入
   transcript_json 却被 sanitizer pop 掉（死代码矛盾）→ sanitizer 不再删
   transcript_json，清理 client_get_meeting 中的重复 pop。
8. **后端管理端 count 优化**：`len(all)` 全表加载改为 `func.count()`。
9. **MCP 工具陈旧字段**：`aimeeting_list_meetings` 引用不存在的
   `meeting_type`/`organizer`、`aimeeting_list_records` 引用不存在的
   `speaker`/`content`/`record_time`（会 AttributeError）→ 改为当前模型字段；
   count 同步改 `func.count()`。

以上已同步运行目录并重启验证（接口 200、MCP 注册正常、前端 build 通过）。

## 10. 已知未修复问题（详见 docs/BUGS_AND_REVIEW.md）

- 页面刷新后 meeting 仍 in_progress：无「恢复录音」路径，用户只能结束会议
- 增量分离 O(n²)（每片全量合并重跑），长会议越来越慢；`m{id}_merged.wav` 同名互踩竞态
- 声纹聚类编号跨轮漂移：用户改名映射可能错位（阈值 0.5 可调）
- Safari（iOS）无法 query 麦克风权限、切后台中断录音
- sliceSeq 已无用；transcript 轮询的 speakers 字段后端不返回（死代码）
- AI 纪要质量依赖 LLM 供应商配置（DeepSeek），无 fallback

## 11. 接手建议（优先级排序）

1. 先跑通一次本地全链路（创建会议→录音 1 分钟→结束→管理端看结果），对照第 2 节数据流理解。
2. 高优先补齐：刷新后「恢复录音」路径（meeting in_progress && !recRecording 时给恢复按钮）。
3. 中优先：`m{id}_merged.wav` 加锁/唯一后缀防互踩；diarization 阈值 0.5 调参
   （会议 26 曾单人被聚成 13 人再收敛到 2 人）。
4. 低优先：清理 sliceSeq 死代码、pollTranscript 死字段、H5 大 chunk 拆分
   （index 1055kB）。

> AI生成