/**
 * 录音工具：封装 MediaRecorder（麦克风录音）
 * 15 秒滚动切片：每 15 秒 stop 当前 recorder → 上传完整 WebM → 立即 start 新 recorder。
 * 关键：Chrome 的 MediaRecorder.start(timeslice) 分片模式下，只有第一个 blob 含 WebM 头，
 * 后续 blob 是裸 Opus 数据，无法独立解码（ffmpeg 会报 Invalid data）。
 * 因此改用「滚动重启」方案，保证每个分片都是完整可解码的 WebM。
 *
 * 权限管理：授权与录音解耦——独立授权按钮先完成 getUserMedia（成功后立即关流），
 * 避免开始录音时才触发弹窗；permissions.query 无感检测三态（prompt/granted/denied），
 * denied 时引导用户去浏览器站点设置重置。
 * 限制：iOS Safari 无法捕获系统声音，只能录麦克风；切后台会中断。
 */
import { ref, onBeforeUnmount } from 'vue'

/** 麦克风权限三态 */
export type MicPermission = 'unknown' | 'checking' | 'prompt' | 'granted' | 'denied' | 'unsupported'

export function useRecorder() {
  const recording = ref(false)
  const paused = ref(false)
  const elapsed = ref(0) // 已录秒数
  const error = ref('')
  const supported = typeof navigator !== 'undefined' && !!navigator.mediaDevices?.getUserMedia && typeof MediaRecorder !== 'undefined'

  // ── 权限状态 ──
  const permission = ref<MicPermission>('unknown')
  const requesting = ref(false) // 正在申请权限

  let permissionWatch: any = null

  /** 无感检测权限状态（不触发弹窗）：优先 permissions.query，降级 unknown */
  async function checkPermission() {
    if (!supported) {
      permission.value = 'unsupported'
      return
    }
    try {
      if (navigator.permissions?.query) {
        permission.value = 'checking'
        const status = await navigator.permissions.query({ name: 'microphone' as PermissionName })
        // Safari 不支持 microphone 权限查询会 throw，走 catch 降级
        mapPermission(status.state)
        // 监听变化（如用户在地址栏图标里改了权限）
        if (permissionWatch) {
          try { permissionWatch.removeEventListener?.('change', permissionWatch._onChange) } catch { /* noop */ }
        }
        permissionWatch = status
        status._onChange = () => mapPermission(status.state)
        status.addEventListener?.('change', status._onChange)
      } else {
        permission.value = 'unknown'
      }
    } catch {
      // iOS Safari 等：无法查询，保持 unknown（不阻塞，点录音时再触发真实申请）
      permission.value = 'unknown'
    }
  }

  function mapPermission(state: string) {
    if (state === 'granted') {
      permission.value = 'granted'
      // 权限实际已就绪时，清掉残留的授权引导类错误（如设备慢导致的超时文案）
      if (error.value && (error.value.includes('授权') || error.value.includes('弹窗') || error.value.includes('权限'))) {
        error.value = ''
      }
    }
    else if (state === 'denied') permission.value = 'denied'
    else permission.value = 'prompt'
  }

  /** 独立授权：正式申请麦克风（弹窗），成功后立即关流（不录音）。
   *  返回 true=已授权 / false=被拒或失败（error 已带引导文案）。 */
  async function requestPermission(): Promise<boolean> {
    if (!supported) {
      permission.value = 'unsupported'
      error.value = '当前浏览器不支持录音，请使用最新版 Safari / Chrome'
      return false
    }
    if (permission.value === 'granted') return true
    requesting.value = true
    error.value = ''
    try {
      // 超时保护：弹窗长时间没点（被遮挡/没注意到）时给出提示而非无限等待
      const streamPromise = navigator.mediaDevices.getUserMedia({ audio: true })
      const timeout = new Promise<never>((_, reject) => {
        window.setTimeout(() => reject(new Error('TIMEOUT')), 12000)
      })
      const stream_ = await Promise.race([streamPromise, timeout])
      // 申请成功，立即释放（仅授权，不占设备）
      stream_.getTracks().forEach((t) => t.stop())
      permission.value = 'granted'
      return true
    } catch (e: any) {
      if (e?.message === 'TIMEOUT') {
        // 弹窗未处理：保持 prompt 态，提示用户留意弹窗
        permission.value = 'prompt'
        error.value = '没有看到授权弹窗吗？请留意浏览器地址栏附近的麦克风弹窗，点击「允许」后重试；若之前拒绝过，请点击地址栏左侧的锁/音符图标修改麦克风权限'
      } else if (e?.name === 'NotAllowedError' || e?.name === 'PermissionDeniedError') {
        permission.value = 'denied'
        error.value = '麦克风权限被拒绝：请点击浏览器地址栏左侧的锁形/音符图标 → 将麦克风设为「允许」→ 刷新页面重试'
      } else if (e?.name === 'NotFoundError') {
        error.value = '未检测到麦克风设备，请检查耳机/麦克风是否连接'
      } else if (e?.name === 'NotReadableError' || e?.name === 'AbortError') {
        error.value = '麦克风被其他应用占用，或系统未授权浏览器使用麦克风（macOS：系统设置 → 隐私与安全性 → 麦克风），请处理后重试'
      } else {
        error.value = '授权失败：' + (e?.message || e)
      }
      return false
    } finally {
      requesting.value = false
      // 同步一次权威状态（Chrome denied 后 query 会立刻返回 denied）
      checkPermission()
    }
  }

  // 组件卸载时清理权限监听
  onBeforeUnmount(() => {
    if (permissionWatch) {
      try { permissionWatch.removeEventListener?.('change', permissionWatch._onChange) } catch { /* noop */ }
    }
  })

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
      // 已知被拒绝：直接给引导文案，不再发 getUserMedia（Chrome 会静默失败）
      if (permission.value === 'denied') {
        error.value = '麦克风权限被拒绝：请点击浏览器地址栏左侧的锁形/音符图标 → 将麦克风设为「允许」→ 刷新页面重试'
        return false
      }
      // 超时保护：部分环境（无音频设备/权限服务异常/弹窗被忽略）getUserMedia 会无限挂起
      const streamPromise = navigator.mediaDevices.getUserMedia({ audio: true })
      const timeout = new Promise<never>((_, reject) => {
        window.setTimeout(() => reject(new Error('TIMEOUT')), 10000)
      })
      stream = await Promise.race([streamPromise, timeout])
      // 开流成功 = 已授权，同步权限态
      if (permission.value !== 'granted') permission.value = 'granted'

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
        permission.value = 'prompt'
        error.value = '麦克风授权未完成：请先点击上方的「授权麦克风」按钮，在弹窗中点「允许」后再开始录音'
        return false
      }
      if (e?.name === 'NotAllowedError' || e?.name === 'PermissionDeniedError') {
        permission.value = 'denied'
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
      // 同步权威权限状态（denied 等场景）
      checkPermission()
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
      sliceTimer = null
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
      // 关键：暂停时清掉了 sliceTimer，恢复必须重新启动，否则切片上传永远停止
      if (sliceTimer === null) {
        sliceTimer = window.setInterval(async () => {
          if (!recording.value || paused.value) return
          try {
            const blob = await stopCurrentSlice()
            if (blob && blob.size > 0 && onSlice) {
              const offsetSec = currentSliceOffset
              await uploadSlice(blob, offsetSec)
            }
            if (recording.value && !paused.value) {
              currentSliceOffset = elapsed.value
              await mediaSliceStart()
            }
          } catch (e) {
            console.error('[useRecorder] slice error', e)
            try { await mediaSliceStart() } catch { /* 忽略 */ }
          }
        }, SLICE_MS)
      }
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

  return {
    supported, recording, paused, elapsed, error,
    start, pause, resume, stop, fmtDuration, setSliceHandler, getCurrentOffset,
    permission, requesting, checkPermission, requestPermission,
  }
}