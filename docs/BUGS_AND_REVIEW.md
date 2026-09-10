---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '69ad7616-1e60-4fcf-849d-f81053de8cac'
  PropagateID: '69ad7616-1e60-4fcf-849d-f81053de8cac'
  ReservedCode1: '8ef144b9-78c9-410a-96e7-e22eb463d6a4'
  ReservedCode2: '8ef144b9-78c9-410a-96e7-e22eb463d6a4'
---

# AiMeeting Bug 排查清单与功能 Review

> 2026-09-11 ｜ 基于全仓库代码通读（backend 7 文件 + frontend-h5 7 文件 + 管理端 3 文件）
> 状态标记：✅ 已修复（本轮）/ ⚠️ 未修复（按优先级列出）/ 💡 改进建议

---

## A. 已修复（本轮提交）

| # | 位置 | 问题 | 修复 |
|---|---|---|---|
| 1 | useRecorder.uploadSlice | 忙时 `return` 静默丢片（慢网络数据丢失） | pendingSlices 队列串行 flush；时长在停止时固化 |
| 2 | Meeting.vue handleStart | 先调 startMeeting 再开麦，麦克风失败会议卡 in_progress | 先 recorder.start() 验证，失败即返回；start 接口失败回滚 |
| 3 | Meeting.vue handleFinish | 未等上传队列清空就 finish，在途分片被漏 | 先 stop 残片上传 + waitForUploads() 再 finish |
| 4 | Meeting.vue onBeforeUnmount | 直接 stop 丢弃残片（丢最后 ≤15s） | fire-and-forget 上传残片 |
| 5 | Meeting.vue avatarText | `/^([A-Z])\d+/` 不匹配「发言者A001」（^锚点失效） | `/(?:^|[^A-Z])([A-Z])\d{3}/` |
| 6 | api.py client_finish | `wait_for_records_done(30s)` 阻塞响应 | 移入后台 finalize_meeting（自带 90s 等待），接口立即返回 |
| 7 | api.py _sanitize_client_meeting | pop transcript_json 与 client_get_meeting 显式保留相矛盾 | sanitizer 不再删该字段 |
| 8 | api.py list_meetings / get_meeting | `len(all)` 全表加载计数 | `func.count()` |
| 9 | mcp_tools.py | 引用不存在的字段（meeting_type/organizer/speaker/content/record_time）→ 调用即 AttributeError | 改为当前模型字段 |

历史已修复（前几轮）：startMeeting 漏调（增量分离永不触发）、merge 清零已回填
speaker、resume 不重启 sliceTimer、MediaRecorder 裸 Opus 分片（滚动重启方案）、
_merge_audio_files 顺序拼接时间轴漂移（numpy 按 offset 补静音）。

## B. 未修复 Bug（按优先级）

### B1（高）：页面刷新后无法恢复录音

- **现象**：录音中刷新页面，`meeting.status` 仍是 `in_progress`，模板显示
  「暂停/结束」按钮，但 recorder 已死。用户没有任何办法继续录音，只能结束。
- **位置**：Meeting.vue 模板 `meeting?.status === 'in_progress' || recRecording` 分支。
- **建议**：检测 `status === 'in_progress' && !recRecording && !finishing` 时显示
  「恢复录音」按钮（重走 handleStart 但跳过 startMeeting，offset 从
  `meeting.audio_duration` 续）。需后端在详情返回 last offset（或前端用
  transcript segments 末尾 end 近似）。

### B2（高）：增量分离的并发竞态

- **现象**：两个转写任务几乎同时完成 → 两个 `run_diarization(incremental=True)`
  并发执行 → `m{id}_merged.wav` 同名互踩；transcript_json 读改写竞态（后写覆盖先写）。
- **位置**：services.py transcribe_record_async → run_diarization；api.py upload 接口
  15s 一片高频触发。
- **建议**：按 meeting_id 加 `asyncio.Lock`（每会议一把，全局 dict 管理）；
  merged 文件名加轮次后缀或临时文件+rename 原子替换。

### B3（高）：声纹聚类编号跨轮漂移

- **现象**：每轮全量重聚类的 speaker_no 不稳定（会议 26 实测：单人先被聚成
  13 人后收敛 2 人）。用户给「发言者A001」改名后，下一轮 A001 可能换人，
  说话人列表改名映射错位。
- **位置**：services.py run_diarization（FastClusteringConfig threshold=0.5）。
- **建议**：短中期用「声纹 embedding 匹配延续」成本高；务实做法：分离成功后
  若 speaker_no 集合与上轮一致（人数相同）才沿用改名映射，否则重置为默认名。
  阈值可试 0.55-0.6 减少过度分裂。也可增量期间只跑一次（首个分片后）+ 会后全量一次。

### B4（中）：会议列表 start_time 为空时排序与展示

- `list_meetings` 按 `start_time.desc().nullslast()` 排序——但 H5 创建的会议
  `start_time=NULL`（不填），实际有效排序靠 id.desc() 兜底，但管理端列表
  「开始时间」列全显示「—」。建议列表展示 `created_at` 或把 start_time
  在创建时默认置为当前时间。

### B5（中）：转写失败无重试，失败记录永久 failed

- transcribe_record_async 异常直接落 failed，无重试机制。建议至少指数退避重试
  1 次（ffmpeg 抖动/模型加载竞争常见瞬时失败）。

### B6（中）：上传接口未真正校验设备绑定时机

- `_check_device` 绑定发生在 lookup（或详情），若用户 A 拿到编号但从未 lookup
  直接调 audio 上传，首个上传者绑定设备。逻辑可用但与产品语义（创建者即绑定）
  略有出入：H5 创建后立刻 lookup，故实际无洞。留意即可。

### B7（中）：时间戳无时区标记

- 后端全部存 UTC（`_now()` 带 tz），SQLite `DateTime(timezone=True)` 实际不存
  tz 信息；前端若直接 `new Date(str)` 不补 'Z' 会当本地时间差 8 小时。
  目前 H5 未展示绝对时间（只展示会议内偏移 mm:ss），未触发；管理端
  `fmtDateTime` 直接字符串截断显示 UTC 时间——**管理端展示的时间是 UTC，
  与用户本地差 8 小时**，接手后建议统一处理（返回带 Z 或前端补解析）。

### B8（低）：vite dev server 无故宕机

- 已发生一次（2026-09-11 凌晨）。表象迷惑（页面 JS 活着、计时器走、API 全挂）。
  排查：`lsof -ti :5177`。建议生产尽快上 nginx 静态托管 dist，摆脱 dev server。

### B9（低）：死代码清理

- useRecorder.sliceSeq 自增后从未使用。
- Meeting.vue pollTranscript 读 `data.speakers`——transcript 接口不返回该字段
  （说话人更新靠 diarization_status 变化触发 refresh），死代码。
- services.py wait_for_records_done 在 finish 去阻塞后仅 finalize 使用（正常）；
  transcribe_audio_file 旧链路保留供测试。
- request.ts timeout 120s：finish 已不阻塞，可降回 30s。

### B10（低）：H5 构建产物偏大

- index chunk 1055kB（element-plus 全量）。建议路由级懒加载已做，但
  element-plus 组件按需引入（unplugin-vue-components）可再砍一半。

### B11（低）：`_transcribe_sync` 硬编码 language="zh"

- 英文会议识别质量下降。建议无 language 参数让 whisper 自动检测（CPU small
  模型前 30s 检测开销可接受），或暴露会议级配置。

### B12（低）：uploadSlice 失败静默

- 队列化后仍静默失败（注释写靠轮询兜底，但轮询只读不重传）。丢片后该 15s
  音频永久丢失。建议失败重试 1 次后给用户 toast。

## C. 功能 Review（对照产品目标）

**目标**：类讯飞听见——录音→实时转写流→声纹区分说话人→存档后台可查。

| 能力 | 现状 | 评价 |
|---|---|---|
| 会议创建/进入 | 编号+设备绑定，自动命名 | ✅ 可用；缺会议列表入口（用户只能靠编号找回，关闭页面后无历史） |
| 录音 | 15s 滚动切片，权限三态 UI | ✅ 可用；暂停/恢复正常；iOS 后台中断不可解 |
| 实时转写 | 15s 延迟出句，气泡流 | ⚠️ 可用但延迟=切片周期；whisper small 中文准确率中等（beam_size=1、vad_filter） |
| 说话人分离 | 增量+会后全量，发言者A001 | ⚠️ 可用但编号漂移（B3）；单人场景易过度分裂 |
| 说话人改名 | 增量保留改名 | ✅ merge 按 (start,end) 保留；但 B3 漂移时错位 |
| 结束存档 | 后台 finalize 全链路 | ✅ 已去阻塞；失败步骤有日志不中断 |
| AI 纪要 | LLM JSON→summary+minutes | ✅ 可用；供应商未配置时报错清晰 |
| 管理端 | 列表/详情/纪要/删除 | ✅ 可用；时间显示 UTC（B7） |
| 无登录安全 | 设备绑定 403 | ⚠️ 编号 8 位随机（36^8≈2.8 万亿）不可枚举；但无速率限制，理论可爆破；上传无鉴权重放（同 device_id 可伪造）。内网/演示场景够用 |

**产品层面最大的两个缺口**（建议下一步）：

1. **用户端没有会议历史**：刷新即失联，只能靠编号找回。建议 H5 首页加
   「我的会议」（按 device_id 查列表，后端加接口）。
2. **实时性**：15s 切片是架构性延迟。要真·实时需换流式 ASR（参考此前的
   FunASR 调研：paraformer-zh-streaming + 3s 切片，延迟可到 3s 级），
   是一次较大的架构升级，建议单独立项。

**AI 纪要质量**：prompt 已要求四板块结构，解析容错（markdown 剥壳 + JSON
截取）完善。改进方向：把说话人改名信息喂给 prompt（当前素材只含编号，
「张三同意了方案」这种归属信息丢失）。

## D. 测试建议（接手后回归清单）

1. 创建会议→开始→录 1 分钟（含两人交替说话）→暂停 10s→继续→结束：
   验证切片连续、offset 无重叠空洞、说话人稳定、纪要生成。
2. 录音中刷新页面：确认 B1 现象（当前预期：只能结束）。
3. 弱网模拟（Chrome DevTools Slow 3G）录 3 分钟：验证上传队列不丢片（本轮修复项）。
4. iOS Safari 真机：权限降级路径 + 切后台中断行为。
5. 管理端：列表筛选/搜索、详情时间线、纪要重新生成、删除会议级联。
6. 并发：两个标签页同设备同会议同时录音（当前未防护，offset 会交错——
   属多端录制场景，建议产品层禁止或后端按 device 会话隔离）。

> AI生成