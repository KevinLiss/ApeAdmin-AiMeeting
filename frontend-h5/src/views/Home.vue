<template>
  <div class="page home-page">
    <div class="hero">
      <div class="logo-badge">
        <el-icon :size="22" color="#fff"><Microphone /></el-icon>
      </div>
      <h1>AI 会议</h1>
    </div>

    <!-- 单一入口面板：进入会议 / 创建会议 模式切换 -->
    <div class="panel">
      <transition name="fade-slide" mode="out-in">
        <!-- 进入会议 -->
        <div v-if="mode === 'join'" key="join" class="panel-body">
          <h2 class="panel-title">进入会议</h2>
          <el-form @submit.prevent="handleLookup">
            <el-input
              v-model="code"
              size="large"
              class="code-input"
              placeholder="请输入会议编号"
              maxlength="32"
              style="text-transform: uppercase"
              @keyup.enter="handleLookup"
            />
            <el-button type="primary" size="large" class="primary-btn" :loading="looking" @click="handleLookup">
              进入会议
            </el-button>
          </el-form>
          <div class="switch-row">
            <span class="text-muted">还没有会议？</span>
            <button class="link-btn" type="button" @click="mode = 'create'">创建会议</button>
          </div>
        </div>

        <!-- 创建会议 -->
        <div v-else key="create" class="panel-body">
          <h2 class="panel-title">创建会议</h2>
          <el-form :model="createForm" label-position="top" @submit.prevent="handleCreate">
            <el-form-item label="会议名称" required>
              <el-input v-model="createForm.title" placeholder="例如：产品周例会" maxlength="200" />
            </el-form-item>
            <el-button type="primary" size="large" class="primary-btn" :loading="creating" @click="handleCreate">
              创建并进入
            </el-button>
          </el-form>
          <div class="switch-row">
            <button class="link-btn" type="button" @click="mode = 'join'">← 返回输入会议编号</button>
          </div>
        </div>
      </transition>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Microphone } from '@element-plus/icons-vue'
import { lookupMeeting, createMeeting, getDeviceId } from '@/api/aimeeting'

const router = useRouter()

/** 当前面板模式：join=进入会议 / create=创建会议 */
const mode = ref<'join' | 'create'>('join')
const code = ref('')
const looking = ref(false)
const creating = ref(false)

const createForm = reactive({
  title: '',
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
    }
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
.home-page {
  display: flex;
  flex-direction: column;
  padding-top: 8px;
}

/* ── 品牌区 ─────────────────────────── */
.hero {
  text-align: center;
  padding: 28px 0 24px;
}
.logo-badge {
  width: 52px;
  height: 52px;
  margin: 0 auto 14px;
  border-radius: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #fff;
  background: linear-gradient(135deg, #4f46e5, #7c3aed);
  box-shadow: 0 8px 20px rgba(79, 70, 229, 0.35);
}
.hero h1 {
  font-size: 26px;
  font-weight: 700;
  letter-spacing: 1px;
}

/* ── 主面板 ── */
.panel {
  background: #fff;
  border-radius: 20px;
  padding: 28px 20px 20px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.08);
}
.panel-title {
  font-size: 20px;
  font-weight: 700;
  margin-bottom: 20px;
  text-align: center;
}

.code-input :deep(.el-input__wrapper) {
  border-radius: 12px;
  padding: 4px 16px;
  min-height: 52px;
  font-size: 16px;
  letter-spacing: 2px;
}
.code-input :deep(.el-input__inner) {
  font-weight: 600;
  text-align: center;
}

/* 主按钮：渐变品牌色 */
.primary-btn {
  width: 100%;
  min-height: 48px;
  margin-top: 16px;
  border: none;
  border-radius: 12px;
  font-size: 16px;
  font-weight: 600;
  letter-spacing: 1px;
  background: linear-gradient(135deg, #4f46e5, #7c3aed);
  box-shadow: 0 6px 16px rgba(79, 70, 229, 0.3);
}
.primary-btn:hover,
.primary-btn:focus {
  background: linear-gradient(135deg, #4338ca, #6d28d9);
}

/* 创建模式表单 */
.panel-body :deep(.el-form-item__label) {
  font-weight: 600;
  font-size: 14px;
}

/* 模式切换 */
.switch-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  margin-top: 18px;
  font-size: 13px;
}
.link-btn {
  border: none;
  background: none;
  padding: 4px 6px;
  color: #4f46e5;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}
.link-btn:active {
  opacity: 0.7;
}

/* 模式切换过渡动画 */
.fade-slide-enter-active,
.fade-slide-leave-active {
  transition: opacity 0.22s ease, transform 0.22s ease;
}
.fade-slide-enter-from {
  opacity: 0;
  transform: translateY(10px);
}
.fade-slide-leave-to {
  opacity: 0;
  transform: translateY(-10px);
}
</style>