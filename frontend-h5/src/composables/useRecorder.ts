/**
 * 录音工具：封装 MediaRecorder（麦克风录音）
 * 限制：iOS Safari 无法捕获系统声音，只能录麦克风；切后台会中断。
 */
import { ref } from 'vue'

export function useRecorder() {
  const recording = ref(false)
  const paused = ref(false)
  const elapsed = ref(0) // 已录秒数
  const error = ref('')
  const supported = typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== 'undefined'

  let mediaRecorder: MediaRecorder | null = null
  let stream: MediaStream | null = null
  let chunks: Blob[] = []
  let timer: number | null = null
  let startTime = 0

  function fmtDuration(sec: number) {
    const s = Math.floor(sec)
    const m = Math.floor(s / 60)
    const r = s % 60
    return `${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}`
  }

  async function start() {
    if (!supported) {
      error.value = '当前浏览器不支持录音，请使用最新版 Safari / Chrome'
      return false
    }
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      // 优先 webm/opus；部分安卓只支持 audio/webm;codecs=opus
      const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : MediaRecorder.isTypeSupported('audio/webm')
          ? 'audio/webm'
          : ''
      mediaRecorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined)
      chunks = []
      mediaRecorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunks.push(e.data)
      }
      mediaRecorder.onstop = () => {
        stopStream()
      }
      mediaRecorder.start(1000) // 每秒收集一次，避免内存占用过大
      recording.value = true
      paused.value = false
      error.value = ''
      startTime = Date.now()
      timer = window.setInterval(() => {
        elapsed.value = Math.floor((Date.now() - startTime) / 1000)
      }, 500)
      return true
    } catch (e: any) {
      if (e?.name === 'NotAllowedError' || e?.name === 'PermissionDeniedError') {
        const isMac = /Mac|iPhone|iPad/.test(navigator.userAgent)
        error.value = isMac
          ? '无法访问麦克风：请在「系统设置 → 隐私与安全性 → 麦克风」中允许浏览器访问麦克风，然后刷新页面重试'
          : '麦克风权限被拒绝：请在浏览器地址栏左侧的权限图标中允许麦克风，然后刷新页面重试'
      } else if (e?.name === 'NotFoundError') {
        error.value = '未检测到麦克风设备，请检查耳机/麦克风是否连接'
      } else if (e?.name === 'NotReadableError' || e?.name === 'AbortError') {
        error.value = '麦克风被其他应用占用，请关闭占用程序后重试'
      } else {
        error.value = '无法启动录音：' + (e?.message || e)
      }
      return false
    }
  }

  function pause() {
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      mediaRecorder.pause()
      paused.value = true
      if (timer) clearInterval(timer)
    }
  }

  function resume() {
    if (mediaRecorder && mediaRecorder.state === 'paused') {
      mediaRecorder.resume()
      paused.value = false
      startTime = Date.now() - elapsed.value * 1000
      timer = window.setInterval(() => {
        elapsed.value = Math.floor((Date.now() - startTime) / 1000)
      }, 500)
    }
  }

  function stop(): Promise<Blob> {
    return new Promise((resolve) => {
      if (!mediaRecorder) {
        resolve(new Blob())
        return
      }
      mediaRecorder.onstop = () => {
        stopStream()
        recording.value = false
        paused.value = false
        if (timer) clearInterval(timer)
        const blob = new Blob(chunks, { type: mediaRecorder?.mimeType || 'audio/webm' })
        chunks = []
        resolve(blob)
      }
      if (mediaRecorder.state !== 'inactive') mediaRecorder.stop()
    })
  }

  function stopStream() {
    if (stream) {
      stream.getTracks().forEach((t) => t.stop())
      stream = null
    }
  }

  return { supported, recording, paused, elapsed, error, start, pause, resume, stop, fmtDuration }
}