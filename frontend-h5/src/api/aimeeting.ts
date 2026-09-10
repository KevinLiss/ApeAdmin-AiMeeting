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

// 创建会议（名称可选，留空自动命名）
export const createMeeting = (data: { title?: string; participants?: string; start_time?: string | null }) =>
  request.post('/client/meetings', data)

// 修改会议名称（全程可改）
export const renameMeeting = (meetingId: number, deviceId: string, title: string) =>
  request.patch(`/client/meetings/${meetingId}`, { title, device_id: deviceId })

// 查询会议详情（含转写进度、纪要状态、说话人）
export const getMeeting = (meetingId: number, deviceId: string) =>
  request.get(`/client/meetings/${meetingId}`, { params: { device_id: deviceId } })

// 轮询最新句级转写（实时对话流）
export const getTranscript = (meetingId: number, deviceId: string) =>
  request.get(`/client/meetings/${meetingId}/transcript`, { params: { device_id: deviceId } })

// 正式开始会议（状态流转 scheduled → in_progress，触发增量声纹分离）
export const startMeeting = (meetingId: number, deviceId: string) =>
  request.post(`/client/meetings/${meetingId}/start`, { device_id: deviceId })

// 上传录音切片（multipart，携带会议内偏移秒数）
export function uploadAudio(
  meetingId: number,
  deviceId: string,
  blob: Blob,
  duration: number,
  offsetSec: number,
) {
  const form = new FormData()
  form.append('file', blob, `rec_${Date.now()}.webm`)
  form.append('device_id', deviceId)
  form.append('duration', String(duration))
  form.append('offset_sec', String(offsetSec))
  return request.post(`/client/meetings/${meetingId}/audio`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  })
}

// 结束会议（触发会后链路：等转写完 → 说话人分离 → AI 纪要）
export function finishMeeting(meetingId: number, deviceId: string) {
  return request.post(`/client/meetings/${meetingId}/finish`, { device_id: deviceId })
}

// 修改说话人显示名称
export function updateSpeaker(
  meetingId: number,
  speakerId: number,
  deviceId: string,
  displayName: string,
) {
  return request.patch(`/client/meetings/${meetingId}/speakers/${speakerId}`, {
    display_name: displayName,
  }, {
    params: { device_id: deviceId },
  })
}