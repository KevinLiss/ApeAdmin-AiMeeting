/**
 * 录音工具：封装 MediaRecorder（麦克风录音）
 * 15 秒滚动切片：每 15 秒 stop 当前 recorder → 上传完整 WebM → 立即 start 新 recorder。
 * 关键：Chrome 的 MediaRecorder.start(timeslice) 分片模式下，只有第一个 blob 含 WebM 头，
 * 后续 blob 是裸 Opus 数据，无法独立解码（ffmpeg 会报 Invalid data）。
 * 因此改用「滚动重启」方案，保证每个分片都是完整可解码的 WebM。
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
  let timer: number | null = null
  let startTime = 0

  // 15 秒切片定时器（到达时停止当前片、上传、重启新片）
  let sliceTimer: number | null = null
  let onSlice: ((blob: Blob, offsetSec: number, durationSec: number) => void) | null = null
  let sliceUploadBusy = false
  let sliceSeq = 0 // 分片序号（从 1 开始）
  let currentSliceOffset = 0 // 当前分片的起始会议偏移（秒），上传时作为 offset_sec

  const SLICE_MS = 15000

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
      // 超时保护：部分环境（无音频设备/权限服务异常）getUserMedia 会无限挂起
      const streamPromise = navigator.mediaDevices.getUserMedia({ audio: true })
      const timeout = new Promise<never>((_, reject) => {
        window.setTimeout(() => reject(new Error('TIMEOUT')), 10000)
      })
      stream = await Promise.race([streamPromise, timeout])

      recording.value = true
      paused.value = false
      error.value = ''
      startTime = Date.now()
      sliceSeq = 0

      // 启动第一个分片
      currentSliceOffset = 0
      await mediaSliceStart()

      // 计时器
      timer = window.setInterval(() => {
        elapsed.value = Math.floor((Date.now() - startTime) / 1000)
      }, 500)

      // 每 15 秒滚动切片：停止当前片 → 上传 → 启动新片
      sliceTimer = window.setInterval(async () => {
        if (!recording.value || paused.value) return
        try {
          const blob = await stopCurrentSlice()
          if (blob && blob.size > 0 && onSlice) {
            const offsetSec = currentSliceOffset
            await uploadSlice(blob, offsetSec)
          }
          // 启动下一片（无论上传是否成功都继续录）
          if (recording.value && !paused.value) {
            currentSliceOffset = elapsed.value
            await mediaSliceStart()
          }
        } catch (e) {
          // 切片失败不中断录音，尝试重启
          console.error('[useRecorder] slice error', e)
          try { await mediaSliceStart() } catch { /* 忽略 */ }
        }
      }, SLICE_MS)
      return true
    } catch (e: any) {
      if (e?.message === 'TIMEOUT') {
        error.value = '麦克风请求超时：请确认浏览器/系统已授予麦克风权限（macOS：系统设置 → 隐私与安全性 → 麦克风），并刷新页面重试'
        return false
      }
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

  /** 启动当前分片 recorder */
  function mediaSliceStart(): Promise<void> {
    return new Promise((resolve, reject) => {
      if (!stream || stream.getAudioTracks().length === 0) {
        reject(new Error('无音频轨道'))
        return
      }
      const mime = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : MediaRecorder.isTypeSupported('audio/webm')
          ? 'audio/webm'
          : ''
      const rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined)
      const chunks: Blob[] = []
      rec.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunks.push(e.data)
      }
      rec.onstop = () => {
        // 由 stopCurrentSlice 处理
      }
      mediaRecorder = rec
      ;(rec as unknown as { __chunks: Blob[] }).__chunks = chunks
      try {
        rec.start()
        sliceSeq += 1
        resolve()
      } catch (err) {
        reject(err)
      }
    })
  }

  /** 停止当前分片，返回完整 blob（等待 onstop 收集） */
  function stopCurrentSlice(): Promise<Blob> {
    return new Promise((resolve) => {
      const rec = mediaRecorder
      if (!rec || rec.state === 'inactive') {
        resolve(new Blob())
        return
      }
      const chunks = (rec as unknown as { __chunks: Blob[] }).__chunks || []
      rec.onstop = () => {
        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' })
        resolve(blob)
      }
      try {
        rec.stop()
      } catch {
        resolve(new Blob())
      }
    })
  }

  /** 串行上传分片，避免并发（失败静默，靠轮询兜底） */
  async function uploadSlice(blob: Blob, offsetSec: number) {
    if (sliceUploadBusy) return
    sliceUploadBusy = true
    try {
      await onSlice?.(blob, offsetSec, Math.floor(elapsed.value))
    } catch {
      // 上传失败不打断录音，等待下一次分片或结束上传兜底
    } finally {
      sliceUploadBusy = false
    }
  }

  /** 注册分片上传回调（每次自动切片/手动结束时调用） */
  function setSliceHandler(fn: (blob: Blob, offsetSec: number, durationSec: number) => void) {
    onSlice = fn
  }

  function pause() {
    if (mediaRecorder && mediaRecorder.state === 'recording') {
      mediaRecorder.pause()
      paused.value = true
      if (timer) clearInterval(timer)
      if (sliceTimer) clearInterval(sliceTimer)
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
      if (sliceTimer) clearInterval(sliceTimer)
      sliceTimer = null
      if (!mediaRecorder) {
        recording.value = false
        paused.value = false
        if (timer) clearInterval(timer)
        resolve(new Blob())
        return
      }
      const rec = mediaRecorder
      const chunks = (rec as unknown as { __chunks: Blob[] }).__chunks || []
      rec.onstop = () => {
        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' })
        mediaRecorder = null
        stopStream()
        recording.value = false
        paused.value = false
        if (timer) clearInterval(timer)
        resolve(blob)
      }
      if (rec.state !== 'inactive') rec.stop()
      else {
        mediaRecorder = null
        stopStream()
        recording.value = false
        paused.value = false
        if (timer) clearInterval(timer)
        resolve(new Blob())
      }
    })
  }

  function stopStream() {
    if (stream) {
      stream.getTracks().forEach((t) => t.stop())
      stream = null
    }
  }

  /** 获取当前分片起始偏移（结束会议上传残片时作为 offset_sec） */
  function getCurrentOffset() {
    return currentSliceOffset
  }

  return { supported, recording, paused, elapsed, error, start, pause, resume, stop, fmtDuration, setSliceHandler, getCurrentOffset }
}