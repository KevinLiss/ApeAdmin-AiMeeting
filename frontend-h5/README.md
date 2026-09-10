---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: '98ae6b12-23e2-49af-b98a-780706f8e584'
  PropagateID: '98ae6b12-23e2-49af-b98a-780706f8e584'
  ReservedCode1: 'ac8fa693-b43a-40d6-9eaa-88107faf9991'
  ReservedCode2: 'ac8fa693-b43a-40d6-9eaa-88107faf9991'
---

# ApeAdmin AI 会议 · 用户端 H5

AI 会议助手的用户端（移动端适配）。参与者通过会议编号进入会议，进行录音、上传转写、查看 AI 纪要。

## 技术栈

- Vue 3 + TypeScript + Vite
- Element Plus（移动端组件）
- MediaRecorder 录音（麦克风）
- axios（请求封装，含设备 ID 指纹）

## 开发

```bash
npm install
npm run dev    # 端口 5177，vite proxy 将 /api 转发到 127.0.0.1:8000
```

## 构建

```bash
npm run build  # 产物输出 dist/
```

## 页面

| 路由 | 说明 |
|---|---|
| `/` | 首页：输入会议编号进入会议，或创建新会议 |
| `/meeting/:id` | 会议页：录音控制（开始/停止/时长）、转写状态、AI 纪要展示 |

## 关键实现

- **无感登录**：`src/api/request.ts` 生成并持久化设备指纹（localStorage），会议编号 + 设备标识作为凭证
- **录音**：`src/composables/useRecorder.ts` 封装 MediaRecorder，仅录制麦克风；iOS Safari 无法捕获系统声音，切后台会中断录音
- **设备绑定**：会议绑定首个访问设备，其他设备访问返回 403

## 接口

后端接口前缀 `/api/v1/aimeeting/client/*`，详见项目根 README「接口概览」。

> AI生成