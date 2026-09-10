<template>
  <div class="page meeting-page">
    <!-- 顶部会议信息（标题可编辑） -->
    <el-card shadow="never" class="head-card">
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
      </div>
    </el-card>

    <!-- 录音控制 -->
    <el-card shadow="never" class="ctrl-card">
      <div v-if="!recSupported" class="unsupported">
        <el-alert type="error" :closable="false" show-icon title="当前浏览器不支持录音，请使用最新版 Chrome / Safari" />
      </div>
      <div v-else class="ctrl-box">
        <!-- 未开始 -->
        <template v-if="meeting?.status === 'scheduled' || (!recRecording && meeting?.status !== 'ended' && meeting?.status !== 'in_progress')">
          <!-- 已授权 / 无法检测（Safari 等）：直接开始 -->
          <div v-if="permStatus === 'granted' || permStatus === 'unknown'" class="start-wrap">
            <el-button type="primary" size="large" round class="big-btn" :loading="starting" @click="handleStart">
              <el-icon><VideoPlay /></el-icon> 正式开始会议
            </el-button>
            <div class="ctrl-tip">点击开始录音并计时，语音实时转写为文字</div>
          </div>
          <!-- 未授权（首次使用）：先授权 -->
          <div v-else-if="permStatus === 'prompt'" class="start-wrap">
            <el-button type="primary" size="large" round class="big-btn" :loading="recRequesting" @click="handleAuth">
              <el-icon><Microphone /></el-icon> 授权麦克风
            </el-button>
            <div class="ctrl-tip">首次使用请先授权麦克风，录音仅在本页会议期间进行</div>
            <div v-if="recError" class="rec-error">{{ recError }}</div>
          </div>
          <!-- 被拒绝 -->
          <div v-else-if="permStatus === 'denied'" class="perm-denied">
            <el-alert type="error" :closable="false" show-icon title="麦克风权限被拒绝" description="请点击浏览器地址栏左侧的锁形/音符图标，将麦克风设为「允许」后刷新页面" />
          </div>
          <!-- 不支持 -->
          <div v-else-if="permStatus === 'unsupported'" class="perm-denied">
            <el-alert type="error" :closable="false" show-icon title="当前浏览器不支持录音" description="请使用最新版 Chrome / Safari 打开本页" />
          </div>
          <!-- 检测中 -->
          <div v-else class="start-wrap">
            <el-button type="primary" size="large" round class="big-btn" :loading="true" :disabled="true">
              <el-icon><Loading /></el-icon> 检测麦克风权限...
            </el-button>
            <div class="ctrl-tip">正在检测麦克风权限</div>
          </div>
        </template>

        <!-- 进行中（录音中） -->
        <template v-else-if="meeting?.status === 'in_progress' || recRecording">
          <div class="live-now">
            <div class="timer">{{ recFmtDuration(recElapsed) }}</div>
            <el-tag v-if="transcribing > 0" size="small" type="primary" effect="plain">
              转写中 {{ transcribing }} 段
            </el-tag>
          </div>
          <div class="rec-controls">
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
            <el-button type="danger" size="large" round :loading="finishing" @click="handleFinish">
              <el-icon><Promotion /></el-icon> 结束会议
            </el-button>
          </div>
          <div v-if="recError" class="rec-error">{{ recError }}</div>
          <div class="ctrl-tip">会议内容实时转写中，发言自动区分说话人</div>
        </template>

        <!-- 已结束 -->
        <template v-else-if="meeting?.status === 'ended'">
          <el-tag type="success" size="large" effect="light">会议已结束，记录已存档</el-tag>
        </template>
      </div>
    </el-card>

    <!-- 实时转写流 -->
    <el-card shadow="never" class="chat-card">
      <template #header>
        <div class="card-head">
          <span>实时会议记录</span>
          <el-button size="small" :loading="loadingDetail" @click="refresh">刷新</el-button>
        </div>
      </template>

      <div ref="chatBody" class="chat-body">
        <div
          v-for="(seg, i) in segments"
          :key="i"
          class="msg-row"
          :class="{ 'mine': seg.speaker && seg.speaker === lastSpeakerNo }"
        >
          <div class="msg-avatar" :style="{ background: speakerColor(seg.speaker || 0) }">
            {{ avatarText(seg) }}
          </div>
          <div class="msg-main">
            <div class="msg-head">
              <span class="msg-speaker">{{ seg.speaker_name || (seg.speaker ? '发言者' + seg.speaker : '未知') }}</span>
              <span class="msg-time">{{ fmtOffset(seg.start) }}</span>
            </div>
            <div class="msg-bubble">{{ seg.text }}</div>
          </div>
        </div>
        <el-empty v-if="!loadingDetail && segments.length === 0" description="暂无内容，开始会议后实时转写" :image-size="60" />
      </div>
    </el-card>

    <!-- 说话人列表（可改名） -->
    <el-card v-if="speakers.length > 0" shadow="never" class="speaker-card">
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

    <!-- AI 纪要（保留显示，AI 纪要后续完善） -->
    <el-card v-if="meeting?.status === 'ended' && minutes" shadow="never" class="minutes-card">
      <template #header>
        <div class="card-head"><span>AI 会议纪要</span></div>
      </template>
      <template v-if="minutes?.status === 'success'">
        <el-divider content-position="left">一句话总结</el-divider>
        <p class="summary">{{ minutes.summary }}</p>
        <el-divider content-position="left">详细纪要</el-divider>
        <div class="minutes-body pre-wrap">{{ minutes.minutes }}</div>
      </template>
      <el-alert
        v-else-if="minutes?.status === 'pending'"
        type="warning" :closable="false" show-icon title="纪要生成中，请稍候刷新..." />
      <el-alert
        v-else-if="minutes?.status === 'failed'"
        type="error" :closable="false" show-icon :title="'生成失败：' + (minutes?.error || '')" />
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
import { ref, computed, onMounted, onBeforeUnmount, nextTick } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { EditPen, VideoPause, VideoPlay, Promotion, Loading, Microphone } from '@element-plus/icons-vue'
import {
  getMeeting, getTranscript, uploadAudio, finishMeeting, renameMeeting, updateSpeaker, getDeviceId, startMeeting,
} from '@/api/aimeeting'
import { useRecorder } from '@/composables/useRecorder'

const route = useRoute()
const meetingId = Number(route.params.id)
const deviceId = getDeviceId()

const meeting = ref<any>(null)
const minutes = ref<any>(null)
const segments = ref<any[]>([])
const speakers = ref<any[]>([])
const transcribing = ref(0)
const loadingDetail = ref(false)
const finishing = ref(false)
const starting = ref(false)

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
const recSupported = recorder.supported
const recRecording = recorder.recording
const recPaused = recorder.paused
const recElapsed = recorder.elapsed
const recError = recorder.error
const recFmtDuration = recorder.fmtDuration
const permStatus = recorder.permission
const recRequesting = recorder.requesting

const chatBody = ref<HTMLElement | null>(null)
const lastSpeakerNo = computed(() => {
  const arr = segments.value.filter((s) => s.speaker)
  return arr.length ? arr[arr.length - 1].speaker : 0
})

let pollTimer: number | null = null

function statusType(s?: string) {
  return s === 'in_progress' ? 'warning' : s === 'ended' ? 'success' : s === 'cancelled' ? 'info' : 'primary'
}
function statusText(s?: string) {
  return s === 'scheduled' ? '待开始' : s === 'in_progress' ? '进行中' : s === 'ended' ? '已结束' : s === 'cancelled' ? '已取消' : s || '—'
}
const speakerColors = ['#4f46e5', '#7c3aed', '#0891b2', '#d97706', '#dc2626', '#059669', '#db2777', '#7c3aed']
function speakerColor(no: number) {
  if (!no) return '#909399'
  return speakerColors[(no - 1) % speakerColors.length]
}
function avatarText(seg: any) {
  // 显示发言者编号首字母（A001→A）
  const name = seg.speaker_name || ''
  const m = /^([A-Z])\d+/.exec(name)
  if (m) return m[1]
  return name ? name.charAt(0) : '?'
}
function fmtOffset(start: number) {
  const s = Math.floor(start || 0)
  const m = Math.floor(s / 60)
  return `${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`
}
function fmtSpeak(sec: number) {
  if (!sec) return ''
  const m = Math.floor(sec / 60)
  return m > 0 ? `${m} 分 ${sec % 60} 秒` : `${sec} 秒`
}

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

/** 轮询实时转写流 */
async function pollTranscript() {
  try {
    const data: any = await getTranscript(meetingId, deviceId)
    if (data?.segments) segments.value = data.segments
    transcribing.value = data?.processing_count || 0
    if (data?.speakers?.length > 0) speakers.value = data.speakers
    if (meeting.value && data?.diarization_status && meeting.value.diarization_status !== data.diarization_status) {
      refresh()
    }
    await nextTick()
    scrollBottom()
  } catch {
    // 静默
  }
}

function scrollBottom() {
  if (chatBody.value) {
    chatBody.value.scrollTop = chatBody.value.scrollHeight
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

/** 授权麦克风（首次使用）：成功后立即显示开始按钮 */
async function handleAuth() {
  const ok = await recorder.requestPermission()
  if (ok) ElMessage.success('麦克风已就绪，可以开始会议了')
}

/** 正式开始会议：注册切片上传 + 启动录音 + 开始轮询 */
async function handleStart() {
  starting.value = true
  try {
    // 1. 先调后端 start：状态流转 scheduled → in_progress（增量声纹分离依赖此状态）
    await startMeeting(meetingId, deviceId)
    // 2. 注册分片上传回调：每次自动切片上传一段
    recorder.setSliceHandler(async (blob, offsetSec, durationSec) => {
      try {
        await uploadAudio(meetingId, deviceId, blob, durationSec, offsetSec)
      } catch (e: any) {
        ElMessage.error('切片上传失败：' + (e.message || e))
      }
    })
    const ok = await recorder.start()
    if (ok) {
      ElMessage.success('会议已开始，语音实时转写中')
      startPolling()
      await refresh()
    }
  } finally {
    starting.value = false
  }
}

/** 结束会议：停止录音 → 上传最后一段 → 触发存档 */
async function handleFinish() {
  finishing.value = true
  try {
    const duration = recElapsed.value
    const offset = recorder.getCurrentOffset()
    const blob = await recorder.stop()
    if (blob.size > 0) {
      await uploadAudio(meetingId, deviceId, blob, duration - offset, offset)
    }
    const data: any = await finishMeeting(meetingId, deviceId)
    minutes.value = data
    ElMessage.success('会议已结束，记录已存档')
    await refresh()
    stopPolling()
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
  recorder.checkPermission()
})
onBeforeUnmount(() => {
  stopPolling()
  if (recRecording.value) recorder.stop()
})
</script>

<style scoped>
.meeting-page {
  padding-bottom: 24px;
}
.head-card {
  margin-bottom: 12px;
}
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
  flex-wrap: wrap;
  align-items: center;
}

/* 控制区 */
.ctrl-card {
  margin-bottom: 12px;
}
.ctrl-box {
  text-align: center;
  padding: 6px 0;
}
.start-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}
.big-btn {
  min-width: 200px;
  min-height: 48px;
  font-size: 16px;
  font-weight: 600;
}
.ctrl-tip {
  color: #c0c4cc;
  font-size: 12px;
}
.timer {
  font-size: 40px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
  color: #303133;
  margin-bottom: 6px;
}
.live-now {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
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
.unsupported {
  text-align: center;
  padding: 8px 0;
}

/* 转写流 */
.chat-card {
  margin-bottom: 12px;
}
.chat-body {
  max-height: 56vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 4px 2px;
}
.msg-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.msg-avatar {
  flex-shrink: 0;
  width: 34px;
  height: 34px;
  border-radius: 50%;
  color: #fff;
  font-size: 14px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}
.msg-main {
  max-width: calc(100% - 44px);
}
.msg-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.msg-speaker {
  font-size: 13px;
  font-weight: 600;
  color: #4f46e5;
}
.msg-time {
  font-size: 11px;
  color: #b0b3bd;
}
.msg-bubble {
  margin-top: 3px;
  padding: 10px 14px;
  border-radius: 4px 14px 14px 14px;
  background: #f3f4f8;
  color: #303133;
  line-height: 1.7;
  word-break: break-word;
  font-size: 15px;
  display: inline-block;
  max-width: 100%;
}
.msg-row.mine .msg-bubble {
  background: #e8e6f8;
}
/* 说话人列表 */
.speaker-card {
  margin-bottom: 12px;
}
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
</style>
