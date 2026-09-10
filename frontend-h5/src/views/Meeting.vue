<template>
  <div class="page meeting-page">
    <!-- 顶部会议信息（标题可编辑） -->
    <el-card shadow="never">
      <div class="meeting-head">
        <div class="title-row">
          <h2 class="title-text" @click="openRename">
            {{ meeting?.title || '会议' }}
            <el-icon class="edit-icon" v-if="meeting"><EditPen /></el-icon>
          </h2>
        </div>
        <div class="meta-row">
          <el-tag size="small" effect="plain">{{ meeting?.meeting_code }}</el-tag>
          <el-tag size="small" :type="statusType(meeting?.status)">{{ statusText(meeting?.status) }}</el-tag>
        </div>
        <div v-if="meeting?.participants" class="text-muted" style="margin-top: 6px">
          参会人：{{ meeting.participants }}
        </div>
      </div>
    </el-card>

    <!-- 录音控制（15 秒自动切片上传） -->
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <span>语音录音</span>
          <el-tag v-if="recording" size="small" type="danger" effect="plain">每 15 秒自动上传</el-tag>
        </div>
      </template>
      <div v-if="!recSupported" class="unsupported">
        <el-alert type="error" :closable="false" show-icon title="当前浏览器不支持录音，请使用最新版 Chrome / Safari" />
      </div>
      <template v-else>
        <div class="recorder-box">
          <div class="timer">{{ recFmtDuration(recElapsed) }}</div>
          <div class="rec-state">
            <el-tag :type="recRecording ? (recPaused ? 'warning' : 'danger') : 'info'" size="small">
              {{ recRecording ? (recPaused ? '已暂停' : '录音中') : '未录音' }}
            </el-tag>
            <el-tag v-if="transcribing > 0" size="small" type="primary" effect="plain" style="margin-left: 6px">
              转写中 {{ transcribing }} 段
            </el-tag>
          </div>
          <div class="rec-controls">
            <el-button
              v-if="!recRecording"
              type="danger" size="large" round :loading="starting"
              @click="startRecord"
            >
              <el-icon><Microphone /></el-icon> 开始录音
            </el-button>
            <template v-else>
              <el-button
                v-if="!recPaused"
                type="warning" size="large" round @click="recorder.pause()"
              >
                <el-icon><VideoPause /></el-icon> 暂停
              </el-button>
              <el-button
                v-else
                type="success" size="large" round @click="recorder.resume()"
              >
                <el-icon><VideoPlay /></el-icon> 继续
              </el-button>
              <el-button type="primary" size="large" round :loading="uploading" @click="stopAndFinish">
                <el-icon><Promotion /></el-icon> 结束会议
              </el-button>
            </template>
          </div>
          <div v-if="recError" class="rec-error">{{ recError }}</div>
          <div class="rec-tip">录音自动按 15 秒分片上传并逐段转写，无需手动操作；iOS Safari 仅能录制麦克风声音。</div>
        </div>
      </template>
    </el-card>

    <!-- 实时对话流（轮询句级转写） -->
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <span>实时对话</span>
          <el-button size="small" :loading="loadingDetail" @click="refresh">刷新</el-button>
        </div>
      </template>
      <el-empty v-if="!loadingDetail && segments.length === 0" description="暂无对话内容，开始录音后实时呈现" />
      <div v-else class="chat-stream">
        <div v-for="(seg, i) in segments" :key="i" class="chat-line">
          <span v-if="seg.speaker_name || seg.speaker" class="speaker-tag" :class="'sp-' + (seg.speaker || 0)">
            {{ seg.speaker_name || (seg.speaker ? '说话人' + seg.speaker : '') }}
          </span>
          <span class="chat-text">{{ seg.text }}</span>
        </div>
      </div>
    </el-card>

    <!-- 说话人列表（可改名） -->
    <el-card v-if="speakers.length > 0" shadow="never">
      <template #header>
        <div class="card-head">
          <span>参会说话人</span>
          <span class="text-muted" style="font-size: 12px">点击名称可修改</span>
        </div>
      </template>
      <div class="speaker-list">
        <div v-for="sp in speakers" :key="sp.id" class="speaker-item">
          <span class="speaker-dot" :style="{ background: speakerColor(sp.speaker_no) }"></span>
          <button class="speaker-name" @click="editSpeaker(sp)">{{ sp.display_name }}</button>
          <span class="speaker-time">{{ fmtSpeak(sp.total_speak_sec) }}</span>
        </div>
      </div>
    </el-card>

    <!-- 会议纪要 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <span>AI 会议纪要</span>
          <el-button
            v-if="meeting?.status === 'ended' && minutes?.status !== 'success'"
            type="primary" size="small" :loading="finishing" @click="handleFinish"
          >
            生成纪要
          </el-button>
        </div>
      </template>
      <el-alert
        v-if="minutes?.status === 'failed'"
        type="error" :closable="false" show-icon
        :title="'生成失败：' + (minutes?.error || '')"
        style="margin-bottom: 8px"
      />
      <el-alert
        v-if="minutes?.status === 'pending'"
        type="warning" :closable="false" show-icon
        title="纪要生成中，请稍候刷新..."
        style="margin-bottom: 8px"
      />
      <el-alert
        v-else-if="meeting?.status === 'ended' && (!minutes || minutes.status === 'none')"
        type="info" :closable="false" show-icon
        title="会议已结束，AI 正在生成纪要，请稍候刷新"
        style="margin-bottom: 8px"
      />
      <template v-if="minutes?.status === 'success'">
        <el-divider content-position="left">一句话总结</el-divider>
        <p class="summary">{{ minutes.summary }}</p>
        <el-divider content-position="left">详细纪要</el-divider>
        <div class="minutes-body pre-wrap">{{ minutes.minutes }}</div>
      </template>
      <el-empty v-else-if="!minutes || minutes.status === 'none'" description="暂无纪要" />
    </el-card>

    <!-- 改名弹窗 -->
    <el-dialog v-model="renameVisible" title="修改会议名称" width="88%" append-to-body>
      <el-input v-model="renameText" maxlength="200" placeholder="请输入新的会议名称" @keyup.enter="confirmRename" />
      <template #footer>
        <el-button @click="renameVisible = false">取消</el-button>
        <el-button type="primary" :loading="renaming" @click="confirmRename">确定</el-button>
      </template>
    </el-dialog>

    <!-- 说话人改名弹窗 -->
    <el-dialog v-model="speakerVisible" title="修改说话人名称" width="88%" append-to-body>
      <el-input v-model="speakerText" maxlength="100" placeholder="输入真实姓名或称呼" @keyup.enter="confirmSpeaker" />
      <template #footer>
        <el-button @click="speakerVisible = false">取消</el-button>
        <el-button type="primary" :loading="speakerSaving" @click="confirmSpeaker">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Microphone, VideoPause, VideoPlay, Promotion, EditPen } from '@element-plus/icons-vue'
import {
  getMeeting, getTranscript, uploadAudio, finishMeeting, renameMeeting, updateSpeaker, getDeviceId,
} from '@/api/aimeeting'
import { useRecorder } from '@/composables/useRecorder'

const route = useRoute()
const meetingId = Number(route.params.id)
const deviceId = getDeviceId()

const meeting = ref<any>(null)
const minutes = ref<any>(null)
const segments = ref<any[]>([])
const speakers = ref<any[]>([])
const transcribing = ref(0) // 转写中段数（轮询得到）
const loadingDetail = ref(false)
const finishing = ref(false)
const starting = ref(false)
const uploading = ref(false)

// 改名
const renameVisible = ref(false)
const renameText = ref('')
const renaming = ref(false)
// 说话人改名
const speakerVisible = ref(false)
const speakerText = ref('')
const speakerSaving = ref(false)
const currentSpeaker = ref<any>(null)

const recorder = useRecorder()
// 解构顶层 ref，模板自动解包
const recSupported = recorder.supported
const recRecording = recorder.recording
const recPaused = recorder.paused
const recElapsed = recorder.elapsed
const recError = recorder.error
const recFmtDuration = recorder.fmtDuration

let pollTimer: number | null = null

function statusType(s?: string) {
  return s === 'in_progress' ? 'warning' : s === 'ended' ? 'success' : s === 'cancelled' ? 'info' : 'primary'
}
function statusText(s?: string) {
  return s === 'scheduled' ? '待开始' : s === 'in_progress' ? '进行中' : s === 'ended' ? '已结束' : s === 'cancelled' ? '已取消' : s || '—'
}
function fmtDuration(sec: number) {
  const s = Math.floor(sec || 0)
  const m = Math.floor(s / 60)
  return m > 0 ? `${m} 分 ${s % 60} 秒` : `${s} 秒`
}
const speakerColors = ['#4f46e5', '#7c3aed', '#0891b2', '#d97706', '#dc2626', '#059669', '#db2777', '#7c3aed']
function speakerColor(no: number) {
  return speakerColors[(no - 1) % speakerColors.length]
}

// 会议详情 + 说话人（低频）
async function refresh() {
  loadingDetail.value = true
  try {
    const data: any = await getMeeting(meetingId, deviceId)
    meeting.value = data
    minutes.value = data?.minutes || null
    speakers.value = data?.speakers || []
  } catch (e: any) {
    ElMessage.error(e.message || '加载失败')
  } finally {
    loadingDetail.value = false
  }
}

/** 轮询实时对话流（句级转写 + 转写进度 + 说话人） */
async function pollTranscript() {
  try {
    const data: any = await getTranscript(meetingId, deviceId)
    if (data?.segments) segments.value = data.segments
    transcribing.value = data?.processing_count || 0
    if (data?.speakers?.length > 0) speakers.value = data.speakers
    if (meeting.value && data?.diarization_status && meeting.value.diarization_status !== data.diarization_status) {
      // 说话人分离完成，刷新详情拿最新说话人
      refresh()
    }
  } catch {
    // 轮询失败静默
  }
}

function startPolling() {
  stopPolling()
  pollTimer = window.setInterval(pollTranscript, 5000)
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

/** 开始录音：注册 15 秒切片上传回调 */
async function startRecord() {
  starting.value = true
  try {
    // 注册分片上传回调：每次自动切片上传一段
    recorder.setSliceHandler(async (blob, offsetSec, durationSec) => {
      try {
        await uploadAudio(meetingId, deviceId, blob, durationSec, offsetSec)
      } catch (e: any) {
        ElMessage.error('切片上传失败：' + (e.message || e))
      }
    })
    const ok = await recorder.start()
    if (ok) {
      ElMessage.success('开始录音（15 秒自动分片上传）')
      startPolling()
    }
  } finally {
    starting.value = false
  }
}

/** 结束会议：停止录音 → 上传最后一段 → 触发会后链路 */
async function stopAndFinish() {
  uploading.value = true
  try {
    const duration = recElapsed.value
    const offset = recorder.getCurrentOffset()
    const blob = await recorder.stop()
    if (blob.size > 0) {
      await uploadAudio(meetingId, deviceId, blob, duration - offset, offset)
    }
    // 结束会议（后台生成纪要）
    const data: any = await finishMeeting(meetingId, deviceId)
    minutes.value = data
    ElMessage.success('会议已结束，AI 正在生成纪要')
    await refresh()
    stopPolling()
  } catch (e: any) {
    ElMessage.error(e.message || '结束会议失败')
  } finally {
    uploading.value = false
  }
}

async function handleFinish() {
  finishing.value = true
  try {
    const data: any = await finishMeeting(meetingId, deviceId)
    minutes.value = data
    if (data?.status === 'success') {
      ElMessage.success('会议纪要生成成功')
    } else if (data?.status === 'failed') {
      ElMessage.error('生成失败：' + (data?.error || ''))
    } else {
      ElMessage.info('会议已结束，纪要生成中')
    }
    await refresh()
  } catch (e: any) {
    ElMessage.error(e.message || '结束会议失败')
  } finally {
    finishing.value = false
  }
}

// ── 会议改名 ─────────────────────────────────────────────
function openRename() {
  renameText.value = meeting.value?.title || ''
  renameVisible.value = true
}
async function confirmRename() {
  const title = renameText.value.trim()
  if (!title) {
    ElMessage.warning('会议名称不能为空')
    return
  }
  renaming.value = true
  try {
    await renameMeeting(meetingId, deviceId, title)
    ElMessage.success('会议名称已更新')
    renameVisible.value = false
    await refresh()
  } catch (e: any) {
    ElMessage.error(e.message || '修改失败')
  } finally {
    renaming.value = false
  }
}

// ── 说话人改名 ───────────────────────────────────────────
function editSpeaker(sp: any) {
  currentSpeaker.value = sp
  speakerText.value = sp.display_name || ''
  speakerVisible.value = true
}
async function confirmSpeaker() {
  const name = speakerText.value.trim()
  if (!name) {
    ElMessage.warning('名称不能为空')
    return
  }
  speakerSaving.value = true
  try {
    await updateSpeaker(meetingId, currentSpeaker.value.id, deviceId, name)
    ElMessage.success('说话人名称已更新')
    speakerVisible.value = false
    await refresh()
  } catch (e: any) {
    ElMessage.error(e.message || '修改失败')
  } finally {
    speakerSaving.value = false
  }
}

onMounted(() => {
  refresh()
  startPolling()
})
onBeforeUnmount(() => {
  stopPolling()
  if (recRecording.value) recorder.stop()
})
</script>

<style scoped>
.meeting-head .title-row {
  display: flex;
  align-items: center;
}
.meeting-head h2 {
  font-size: 20px;
  margin-bottom: 8px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.edit-icon {
  cursor: pointer;
  color: #a0aec0;
  font-size: 15px;
}
.meeting-head .meta-row {
  display: flex;
  gap: 6px;
}
.recorder-box {
  text-align: center;
  padding: 8px 0;
}
.timer {
  font-size: 44px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: #303133;
  margin-bottom: 6px;
}
.rec-state {
  margin-bottom: 16px;
}
.rec-controls {
  display: flex;
  justify-content: center;
  gap: 10px;
  flex-wrap: wrap;
}
.rec-error {
  margin-top: 10px;
  color: var(--el-color-danger);
  font-size: 13px;
}
.rec-tip {
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px dashed #ebeef5;
  color: #c0c4cc;
  font-size: 12px;
  line-height: 1.6;
}
/* 实时对话流 */
.chat-stream {
  max-height: 340px;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.chat-line {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.speaker-tag {
  flex-shrink: 0;
  padding: 2px 8px;
  border-radius: 8px;
  color: #fff;
  font-size: 12px;
  font-weight: 600;
  background: #909399;
}
.chat-text {
  color: #303133;
  line-height: 1.7;
  word-break: break-word;
}
/* 说话人列表 */
.speaker-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.speaker-item {
  display: flex;
  align-items: center;
  gap: 10px;
}
.speaker-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.speaker-name {
  border: none;
  background: none;
  color: #303133;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  padding: 2px 4px;
}
.speaker-name:active {
  opacity: 0.6;
}
.speaker-time {
  margin-left: auto;
  color: #909399;
  font-size: 12px;
}
.summary {
  font-size: 15px;
  font-weight: 600;
  color: #303133;
  line-height: 1.6;
}
.minutes-body {
  color: #303133;
  line-height: 1.7;
}
.pre-wrap {
  white-space: pre-wrap;
  word-break: break-word;
}
.unsupported {
  text-align: center;
  padding: 8px 0;
}
</style>