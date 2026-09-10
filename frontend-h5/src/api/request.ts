/**
 * H5 用户端 API 封装
 * 开发环境经 vite proxy 转发，生产环境直连后端（同域或配置了 CORS）。
 */
import axios from 'axios'

const request = axios.create({
  baseURL: '/api/v1/aimeeting',
  timeout: 120000, // 转写/纪要可能较慢
})

request.interceptors.response.use(
  (res) => {
    // 后端统一包装 { code, msg, data }
    const body = res.data
    if (body && typeof body === 'object' && 'code' in body) {
      if (body.code === 200) return body.data
      return Promise.reject(new Error(body.msg || '请求失败'))
    }
    return body
  },
  (err) => {
    const detail = err.response?.data?.detail || err.message || '网络错误'
    return Promise.reject(new Error(detail))
  }
)

export default request