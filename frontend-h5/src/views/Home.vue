<template>
  <div class="page home-page">
    <div class="hero">
      <h1>AI 会议</h1>
      <p class="text-muted">输入会议编号进入会议，支持语音录音、自动转写与 AI 纪要</p>
    </div>

    <!-- 无感登录：输入会议编号 -->
    <el-card shadow="never">
      <template #header>进入会议</template>
      <el-form @submit.prevent="handleLookup">
        <el-form-item>
          <el-input
            v-model="code"
            size="large"
            placeholder="输入 8 位会议编号"
            maxlength="32"
            style="text-transform: uppercase"
            @keyup.enter="handleLookup"
          />
        </el-form-item>
        <el-button type="primary" size="large" style="width: 100%" :loading="looking" @click="handleLookup">
          进入会议
        </el-button>
      </el-form>
      <div class="tip text-muted">无账号即可使用，输入会议编号自动进入</div>
    </el-card>

    <!-- 创建会议 -->
    <el-card shadow="never">
      <template #header>创建会议</template>
      <el-form :model="createForm" label-position="top">
        <el-form-item label="会议名称" required>
          <el-input v-model="createForm.title" placeholder="例如：产品周例会" maxlength="200" />
        </el-form-item>
        <el-form-item label="参会人（可选）">
          <el-input v-model="createForm.participants" placeholder="多个参会人用逗号分隔" />
        </el-form-item>
        <el-form-item label="开始时间（可选）">
          <el-date-picker
            v-model="createForm.start_time"
            type="datetime"
            placeholder="选择时间"
            style="width: 100%"
          />
        </el-form-item>
        <el-button type="primary" plain size="large" style="width: 100%" :loading="creating" @click="handleCreate">
          创建会议并进入
        </el-button>
      </el-form>
    </el-card>

    <!-- 录音功能提示 -->
    <el-alert
      type="info"
      :closable="false"
      show-icon
      title="录音说明：仅录制麦克风声音；iOS Safari 无法录制系统播放的声音。切到后台会中断录音。"
      style="margin-top: 4px"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { lookupMeeting, createMeeting, getDeviceId } from '@/api/aimeeting'

const router = useRouter()
const code = ref('')
const looking = ref(false)
const creating = ref(false)

const createForm = reactive({
  title: '',
  participants: '',
  start_time: null as string | null,
})

async function handleLookup() {
  const c = code.value.trim().toUpperCase()
  if (!c) {
    ElMessage.warning('请输入会议编号')
    return
  }
  looking.value = true
  try {
    const data: any = await lookupMeeting(c, getDeviceId())
    ElMessage.success(`已进入会议「${data.title}」`)
    router.push(`/meeting/${data.id}`)
  } catch (e: any) {
    ElMessage.error(e.message || '查询失败')
  } finally {
    looking.value = false
  }
}

async function handleCreate() {
  if (!createForm.title.trim()) {
    ElMessage.warning('请输入会议名称')
    return
  }
  creating.value = true
  try {
    const payload: any = {
      title: createForm.title.trim(),
      participants: createForm.participants,
    }
    if (createForm.start_time) payload.start_time = createForm.start_time
    const data: any = await createMeeting(payload)
    ElMessage.success(`会议创建成功，编号：${data.meeting_code}`)
    // 创建后自动绑定设备并进入
    await lookupMeeting(data.meeting_code, getDeviceId())
    router.push(`/meeting/${data.id}`)
  } catch (e: any) {
    ElMessage.error(e.message || '创建失败')
  } finally {
    creating.value = false
  }
}
</script>

<style scoped>
.hero {
  text-align: center;
  padding: 24px 0 8px;
}
.hero h1 {
  font-size: 28px;
  font-weight: 700;
  background: linear-gradient(135deg, #4f46e5, #7c3aed);
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
  margin-bottom: 6px;
}
.tip {
  margin-top: 10px;
  font-size: 12px;
  color: #c0c4cc;
  text-align: center;
}
</style>