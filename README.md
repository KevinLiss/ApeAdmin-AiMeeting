---
AIGC:
  ContentProducer: '001191110102MAD55U9H0F10002'
  ContentPropagator: '001191110102MAD55U9H0F10002'
  Label: '1'
  ProduceID: 'dcaf0de7-ce9d-41d0-92da-4972679c9a14'
  PropagateID: 'dcaf0de7-ce9d-41d0-92da-4972679c9a14'
  ReservedCode1: 'c46218c2-c77a-40ba-aa36-6b77ca41b4fe'
  ReservedCode2: 'c46218c2-c77a-40ba-aa36-6b77ca41b4fe'
---

# ApeAdmin AI 会议助手（ApeAdmin-AiMeeting）

录音转写 + AI 会议纪要插件。会议参与者通过 H5 用户端开始录音，上传音频后由 faster-whisper 本地转写，再由 LLM（DeepSeek）生成一句话总结与结构化会议纪要（结论 / 讨论要点 / 决议 / 遗留问题）。管理端（ApeAdmin）负责会议历史、录音转写记录与 AI 纪要的查看与管理。

## 功能特性

- **用户端 H5**（`frontend-h5/`）：输入会议编号进入会议 → 录音（MediaRecorder 麦克风）→ 上传转写 → 查看一句话总结与结构化纪要
- **无感登录**：会议编号 + 设备标识（localStorage 持久化指纹）作为凭证，不依赖管理端账号体系
- **本地转写**：faster-whisper（small 模型，约 466MB）离线转写，录音不上传第三方
- **AI 纪要**：DeepSeek 生成一句话总结 + 结构化纪要（结论 / 讨论要点 / 决议 / 遗留问题）
- **管理端**：会议列表（筛选 / 状态流转）、详情（转写记录时间线 + 完整转写文本 + AI 纪要）、系统设置「AI 会议 H5 地址」配置项

## 项目结构

```
backend/                         # ApeAdmin 后端插件（拷贝回主项目 backend/src/plugins/builtin/ 使用）
  └── src/plugins/builtin/aimeeting/
      ├── plugin.py              # 插件入口（安装建表 / 卸载 / 注册路由与 MCP 工具）
      ├── models.py             # 会议 / 转写记录 / 纪要 三张表
      ├── schemas.py            # Pydantic 模型
      ├── api.py                # 管理端 + 用户端 REST API
      ├── services.py           # 转写 / AI 纪要业务逻辑
      ├── seed.py               # 菜单与权限种子数据
      └── plugin.json           # 插件元数据

frontend/                       # ApeAdmin 管理端（拷贝回 ApeAdmin frontend/src/ 使用）
  └── src/
      ├── api/aimeeting.ts          # 管理端 API 封装
      └── views/aimeeting/
          ├── list/index.vue        # 会议列表页
          └── detail/index.vue      # 会议详情页（转写 + 纪要）

frontend-h5/                    # 用户端 H5 独立工程（Vue3 + Vite + Element Plus）
  └── src/
      ├── views/Home.vue        # 首页：会议编号进入 / 创建会议
      ├── views/Meeting.vue     # 会议页：录音控制 + 转写 + 纪要展示
      ├── composables/useRecorder.ts  # 录音 composable（MediaRecorder）
      └── api/aimeeting.ts      # 用户端接口封装（含设备 ID 指纹）
```

## 快速开始

### 1. 后端插件

将 `backend/src/plugins/builtin/aimeeting/` 目录拷贝到 ApeAdmin 项目的 `backend/src/plugins/builtin/aimeeting/`，重启后端自动注册插件（安装时自动建表 + 注入菜单）。

```bash
pip install faster-whisper   # 语音转写依赖（另需下载模型到 backend/models/faster-whisper-small/）
```

启动后端（macOS 本机需绕过系统代理，否则外部 AI 调用会失败）：

```bash
NO_PROXY='*' uvicorn src.main:app --host 0.0.0.0 --port 8000
```

在「AI 助手 → 模型密钥管理」中配置有效的 DeepSeek API Key 后，会议纪要将自动生成。

### 2. 管理端

将 `frontend/src/api/aimeeting.ts` 拷贝到 `frontend/src/api/`，将 `frontend/src/views/aimeeting/` 拷贝到 `frontend/src/views/`，并在 `frontend/src/router/index.ts` 静态路由中注册详情页：

```ts
{ path: 'aimeeting/detail/:id', name: 'AimeetingDetail',
  component: () => import('@/views/aimeeting/detail/index.vue'),
  meta: { title: '会议详情' } },
```

系统设置页（`frontend/src/views/system/settings/index.vue`）新增「AI 会议 H5 地址」配置项（key = `aimeeting_h5_url`），用于展示 H5 访问入口。

### 3. 用户端 H5

```bash
cd frontend-h5
npm install
npm run dev       # 开发端口 5177，vite proxy 转发到 127.0.0.1:8000
npm run build     # 生产构建
```

## 接口概览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/aimeeting/meetings` | 创建会议（管理端） |
| GET | `/api/v1/aimeeting/meetings` | 会议列表（管理端，分页/筛选） |
| GET | `/api/v1/aimeeting/meetings/{id}` | 会议详情（管理端） |
| POST | `/api/v1/aimeeting/meetings/{id}/start` | 开始会议（记录实际开始时间） |
| POST | `/api/v1/aimeeting/meetings/{id}/finish` | 结束会议（记录实际结束时间 + 触发 AI 纪要） |
| POST | `/api/v1/aimeeting/client/meetings` | 用户端创建会议 |
| POST | `/api/v1/aimeeting/client/lookup` | 用户端按编号查会议并绑定设备 |
| GET | `/api/v1/aimeeting/client/meetings/{id}` | 用户端会议详情 |
| POST | `/api/v1/aimeeting/client/meetings/{id}/upload` | 用户端上传录音 → 触发转写 |
| POST | `/api/v1/aimeeting/client/meetings/{id}/finish` | 用户端结束会议 |
| GET | `/api/v1/aimeeting/client/meetings/{id}/minutes` | 用户端获取纪要 |

## 已知约束

- **录音**：仅录制麦克风声音（iOS Safari 无法捕获系统声音，切后台会中断录音）
- **设备绑定保护**：会议绑定首个访问设备后，其他设备访问返回 403「该会议已绑定其他设备」
- **CORS**：生产部署时需在 `backend/src/core/config.py` 的 `CORS_ORIGINS` 中加入 H5 实际域名

## License

MIT

> AI生成