<template>
  <div class="page meeting-page">
    <!-- 顶部会议信息 -->
    <el-card shadow="never">
      <div class="meeting-head">
        <h2>{{ meeting?.title || '会议' }}</h2>
        <div class="meta-row">
          <el-tag size="small" effect="plain">{{ meeting?.meeting_code }}</el-tag>
          <el-tag size="small" :type="statusType(meeting?.status)">{{ statusText(meeting?.status) }}</el-tag>
        </div>
        <div v-if="meeting?.participants" class="text-muted" style="margin-top: 6px">
          参会人：{{ meeting.participants }}
        </div>
      </div>
    </el-card>

    <!-- 录音控制 -->
    <el-card shadow="never">
      <template #header>语音录音</template>
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
              <el-button type="primary" size="large" round :loading="uploading" @click="stopAndUpload">
                <el-icon><Promotion /></el-icon> 结束并上传
              </el-button>
            </template>
          </div>
          <div v-if="recError" class="rec-error">{{ recError }}</div>
          <div class="rec-tip">仅录制麦克风声音；iOS Safari 无法录制系统播放的声音，切到后台会中断录音。</div>
        </div>
      </template>
    </el-card>

    <!-- 转写结果 -->
    <el-card shadow="never">
      <template #header>
        <div class="card-head">
          <span>语音转写</span>
          <el-button size="small" :loading="loadingDetail" @click="refresh">刷新</el-button>
        </div>
      </template>
      <el-empty v-if="!loadingDetail && !transcript" description="暂无转写内容，录音上传后将在此显示" />
      <div v-else class="transcript pre-wrap">{{ transcript }}</div>
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
        title="会议已结束，点击「生成纪要」由 AI 自动总结"
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
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Microphone, VideoPause, VideoPlay, Promotion } from '@element-plus/icons-vue'
import { getMeeting, uploadAudio, finishMeeting, getDeviceId } from '@/api/aimeeting'
import { useRecorder } from '@/composables/useRecorder'

const route = useRoute()
const meetingId = Number(route.params.id)
const deviceId = getDeviceId()

const meeting = ref<any>(null)
const minutes = ref<any>(null)
const transcript = ref('')
const loadingDetail = ref(false)
const finishing = ref(false)
const starting = ref(false)
const uploading = ref(false)

const recorder = useRecorder()
// 解构顶层 ref，模板自动解包
const recSupported = recorder.supported
const recRecording = recorder.recording
const recPaused = recorder.paused
const recElapsed = recorder.elapsed
const recError = recorder.error
const recFmtDuration = recorder.fmtDuration

function statusType(s?: string) {
  return s === 'in_progress' ? 'warning' : s === 'ended' ? 'success' : s === 'cancelled' ? 'info' : 'primary'
}
function statusText(s?: string) {
  return s === 'scheduled' ? '待开始' : s === 'in_progress' ? '进行中' : s === 'ended' ? '已结束' : s === 'cancelled' ? '已取消' : s || '—'
}

async function refresh() {
  loadingDetail.value = true
  try {
    const data: any = await getMeeting(meetingId, deviceId)
    meeting.value = data
    transcript.value = data?.transcript_text || ''
    minutes.value = data?.minutes || null
  } catch (e: any) {
    ElMessage.error(e.message || '加载失败')
  } finally {
    loadingDetail.value = false
  }
}

async function startRecord() {
  starting.value = true
  try {
    const ok = await recorder.start()
    if (ok) ElMessage.success('开始录音')
  } finally {
    starting.value = false
  }
}

async function stopAndUpload() {
  uploading.value = true
  try {
    const duration = recElapsed.value
    const blob = await recorder.stop()
    if (blob.size === 0) {
      ElMessage.warning('未录到音频')
      return
    }
    ElMessage.info('正在上传并转写，请稍候...')
    const res: any = await uploadAudio(meetingId, deviceId, blob, duration)
    if (res?.transcript) {
      transcript.value = res.full_transcript || res.transcript
    }
    ElMessage.success('转写完成')
    await refresh()
  } catch (e: any) {
    ElMessage.error(e.message || '上传失败')
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
      ElMessage.info('会议已结束')
    }
    await refresh()
  } catch (e: any) {
    ElMessage.error(e.message || '结束会议失败')
  } finally {
    finishing.value = false
  }
}

onMounted(refresh)
onBeforeUnmount(() => {
  if (recRecording.value) recorder.stop()
})
</script>

<style scoped>
.meeting-head h2 {
  font-size: 20px;
  margin-bottom: 8px;
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
.transcript {
  color: #303133;
  line-height: 1.7;
  max-height: 260px;
  overflow: auto;
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