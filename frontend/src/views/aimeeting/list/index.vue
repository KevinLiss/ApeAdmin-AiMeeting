<template>
  <div class="aimeeting-page">
    <div class="page-header">
      <h2>AI 会议</h2>
      <p class="text-muted">管理历史会议：查看用户端录制的语音转写文本与 AI 生成的会议纪要</p>
    </div>

    <!-- 操作栏 -->
    <div class="toolbar">
      <el-button type="primary" @click="openCreate" v-permission="'aimeeting:meeting:create'">
        <el-icon><Plus /></el-icon> 新建会议
      </el-button>
      <el-button @click="fetchList" :loading="loading">
        <el-icon><Refresh /></el-icon> 刷新
      </el-button>
      <el-select v-model="filters.status" placeholder="状态" clearable style="width: 130px" @change="fetchList">
        <el-option label="待开始" value="scheduled" />
        <el-option label="进行中" value="in_progress" />
        <el-option label="已结束" value="ended" />
        <el-option label="已取消" value="cancelled" />
      </el-select>
      <el-input
        v-model="filters.keyword"
        placeholder="搜索标题 / 会议编号"
        clearable
        style="width: 220px"
        @keyup.enter="fetchList"
        @clear="fetchList"
      >
        <template #append>
          <el-button :icon="Search" @click="fetchList" />
        </template>
      </el-input>
    </div>

    <!-- 列表 -->
    <el-table :data="tableData" v-loading="loading" stripe style="width: 100%; margin-top: 16px">
      <el-table-column prop="id" label="ID" width="60" />
      <el-table-column prop="title" label="会议标题" min-width="160" show-overflow-tooltip />
      <el-table-column label="会议编号" width="120">
        <template #default="{ row }">
          <el-tag size="small" effect="plain">{{ row.meeting_code }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag :type="statusType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="开始时间" min-width="150">
        <template #default="{ row }">
          <span>{{ fmtDateTime(row.start_time) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="录音时长" width="90" align="center">
        <template #default="{ row }">
          <span>{{ fmtDuration(row.audio_duration) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="转写" width="90" align="center">
        <template #default="{ row }">
          <el-tag :type="transcriptType(row.transcript_status)" size="small">
            {{ transcriptText(row.transcript_status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="纪要" width="90" align="center">
        <template #default="{ row }">
          <el-tag :type="minutesType(row.minutes_status)" size="small">
            {{ minutesText(row.minutes_status) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="140" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="goDetail(row)">详情</el-button>
          <el-button link type="danger" size="small" @click="handleDelete(row)" v-permission="'aimeeting:meeting:delete'">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 分页 -->
    <div class="pagination">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[10, 20, 50]"
        layout="total, sizes, prev, pager, next"
        @size-change="fetchList"
        @current-change="fetchList"
      />
    </div>

    <!-- 新建会议弹窗 -->
    <el-dialog v-model="dialogVisible" title="新建会议" width="520px">
      <el-form :model="form" label-width="90px">
        <el-form-item label="会议标题" required>
          <el-input v-model="form.title" placeholder="请输入会议标题" maxlength="200" />
        </el-form-item>
        <el-form-item label="参会人">
          <el-input v-model="form.participants" placeholder="多个参会人请用逗号分隔" />
        </el-form-item>
        <el-form-item label="会议时间">
          <el-date-picker
            v-model="form.start_time"
            type="datetime"
            placeholder="选择开始时间"
            style="width: 100%"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh, Search } from '@element-plus/icons-vue'
import request from '@/api/request'

const router = useRouter()
const loading = ref(false)
const saving = ref(false)
const tableData = ref<any[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const dialogVisible = ref(false)

const filters = reactive({
  status: '',
  keyword: '',
})

const form = reactive({
  title: '',
  participants: '',
  start_time: null as string | null,
})

function statusType(s: string) {
  return s === 'in_progress' ? 'warning' : s === 'ended' ? 'success' : s === 'cancelled' ? 'info' : 'primary'
}
function statusText(s: string) {
  return s === 'scheduled' ? '待开始' : s === 'in_progress' ? '进行中' : s === 'ended' ? '已结束' : s === 'cancelled' ? '已取消' : s
}
function transcriptType(s?: string) {
  return s === 'success' ? 'success' : s === 'failed' ? 'danger' : s === 'processing' ? 'warning' : 'info'
}
function transcriptText(s?: string) {
  return s === 'success' ? '已完成' : s === 'failed' ? '失败' : s === 'processing' ? '转写中' : '未转写'
}
function minutesType(s?: string) {
  return s === 'success' ? 'success' : s === 'failed' ? 'danger' : s === 'pending' ? 'warning' : 'info'
}
function minutesText(s?: string) {
  return s === 'success' ? '已生成' : s === 'failed' ? '失败' : s === 'pending' ? '生成中' : '未生成'
}
function fmtDateTime(v?: string | null) {
  if (!v) return '—'
  return String(v).replace('T', ' ').slice(0, 16)
}
function fmtDuration(sec?: number) {
  if (!sec) return '—'
  const s = Math.floor(sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  if (h > 0) return `${h}时${m}分`
  if (m > 0) return `${m}分${r}秒`
  return `${r}秒`
}

async function fetchList() {
  loading.value = true
  try {
    const params: any = { page: page.value, page_size: pageSize.value }
    if (filters.status) params.status = filters.status
    if (filters.keyword) params.keyword = filters.keyword
    const res: any = await request.get('/aimeeting/meetings', { params })
    tableData.value = res.items || []
    total.value = res.total || 0
  } catch {
    // handled by interceptor
  } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(form, { title: '', participants: '', start_time: null })
  dialogVisible.value = true
}

async function handleSave() {
  if (!form.title.trim()) {
    ElMessage.warning('请输入会议标题')
    return
  }
  saving.value = true
  try {
    const payload: any = {
      title: form.title.trim(),
      participants: form.participants,
    }
    if (form.start_time) payload.start_time = form.start_time
    await request.post('/aimeeting/meetings', payload)
    ElMessage.success('创建成功')
    dialogVisible.value = false
    await fetchList()
  } catch {
    // handled by interceptor
  } finally {
    saving.value = false
  }
}

async function handleDelete(row: any) {
  await ElMessageBox.confirm(`确定删除会议「${row.title}」吗？删除后其录音转写记录与纪要一并删除。`, '提示', { type: 'warning' })
  await request.delete(`/aimeeting/meetings/${row.id}`)
  ElMessage.success('删除成功')
  await fetchList()
}

function goDetail(row: any) {
  router.push(`/aimeeting/detail/${row.id}`)
}

onMounted(() => {
  fetchList()
})
</script>

<style scoped>
.aimeeting-page {
  padding: 20px;
}
.page-header {
  margin-bottom: 16px;
}
.page-header h2 {
  margin: 0 0 4px;
  font-size: 20px;
}
.page-header .text-muted {
  color: #999;
  font-size: 13px;
  margin: 0;
}
.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.pagination {
  margin-top: 16px;
  display: flex;
  justify-content: flex-end;
}
</style>