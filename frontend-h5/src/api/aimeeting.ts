/**
 * AI 会议用户端 API
 */
import request from './request'

// 设备标识：持久化在 localStorage，作为无感登录凭证
const DEVICE_KEY = 'aimeeting_device_id'

export function getDeviceId(): string {
  let id = localStorage.getItem(DEVICE_KEY)
  if (!id) {
    id = generateDeviceId()
    localStorage.setItem(DEVICE_KEY, id)
  }
  return id
}

function generateDeviceId(): string {
  const rand = Math.random().toString(36).slice(2, 10)
  const ts = Date.now().toString(36)
  return `dev_${ts}_${rand}`
}

// 查询会议（会议编号 + 设备标识）
export const lookupMeeting = (meetingCode: string, deviceId: string) =>
  request.post('/client/meetings/lookup', { meeting_code: meetingCode, device_id: deviceId })

// 创建会议
export const createMeeting = (data: { title: string; participants?: string; start_time?: string | null }) =>
  request.post('/client/meetings', data)

// 查询会议详情（含转写进度、纪要状态）
export const getMeeting = (meetingId: number, deviceId: string) =>
  request.get(`/client/meetings/${meetingId}`, { params: { device_id: deviceId } })

// 上传录音（multipart）
export function uploadAudio(meetingId: number, deviceId: string, blob: Blob, duration: number) {
  const form = new FormData()
  form.append('file', blob, `rec_${Date.now()}.webm`)
  form.append('device_id', deviceId)
  form.append('duration', String(duration))
  return request.post(`/client/meetings/${meetingId}/audio`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}

// 结束会议（触发 AI 纪要）
export function finishMeeting(meetingId: number, deviceId: string) {
  return request.post(`/client/meetings/${meetingId}/finish`, { device_id: deviceId })
}