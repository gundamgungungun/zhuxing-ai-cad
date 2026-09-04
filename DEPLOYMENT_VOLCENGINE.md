# 铸形部署到火山引擎 veFaaS

本文对应当前仓库的自带密钥（BYOK）部署。每位访客需要输入自己的模型服务
API Key；站点本身不配置模型密钥，访客密钥仅保留在当前页面内存，不会写入
浏览器持久存储、服务端文件、数据库或任务日志。

## 1. 本地检查

```bash
conda run -n multi_agent_cad pytest -q tests/test_web_security.py
conda run -n multi_agent_cad python -m compileall -q multi_agent_cad
```

安装 Docker Desktop 后执行：

```bash
docker build -t formforge:0.1.0 .

docker run --rm -p 8000:8000 \
  -e MAC_ALLOW_CLIENT_API_KEY=true \
  formforge:0.1.0

curl http://127.0.0.1:8000/api/config/schema
curl http://127.0.0.1:8000/api/health
```

模型密钥由访客在网页中输入，不得写入 Dockerfile、仓库、镜像标签或 veFaaS
环境变量。

## 2. CLI

```bash
npx @volcengine/vefaas-cli@latest install
vefaas --version
vefaas login
vefaas login --check
```

优先选择浏览器 SSO 登录，避免将 AK/SK 留在终端历史中。

## 3. 云资源

所有资源必须选择同一地域：

1. veFaaS 函数服务，并完成跨服务授权。
2. 镜像仓库 CR：实例、命名空间、OCI 仓库。
3. API 网关：负责 HTTPS、身份认证和限流。
4. TOS：后续用于持久化 STEP、STL、GLB 和质检报告。
5. 日志服务 TLS。

## 4. 构建和推送镜像

```bash
export IMAGE_NAME="<实例域名>/nonstandard/formforge:0.1.0"

docker build -t "$IMAGE_NAME" .
docker login --username="<仓库用户名>" "<实例域名>"
docker push "$IMAGE_NAME"
```

不得把镜像仓库密码作为命令参数写入 Shell 历史；在 Docker 提示后交互输入。

## 5. 创建 Web 应用函数

控制台路径：函数服务 → 函数 → 函数管理 → 创建函数 → Web 应用函数。

建议首轮配置：

| 配置项 | 值 |
|---|---|
| 部署方式 | 镜像仓库 |
| Webserver 模式 | 是 |
| 启动命令 | `/opt/application/deploy/run.sh` |
| 监听端口 | `8000` |
| 实例规格 | 4 vCPU / 8 GiB 起步 |
| 单实例并发 | 1 |
| 执行超时 | 900 秒 |
| 实例数下限 | 内部稳定演示为 1；省钱测试为 0 |
| 实例数上限 | 状态外置前为 1 |
| 日志 | 开启 |

环境变量：

```text
MAC_DEPLOYMENT_MODE=production
MAC_ALLOW_CLIENT_API_KEY=true
MAC_WEB_HOST=0.0.0.0
MAC_WEB_PORT=8000
MAC_MAX_ACTIVE_JOBS=1
```

## 6. 发布与验收

1. 发布 `Latest`，首发采用全量发布。
2. 从 API 网关访问 `/api/health`，应返回 `{"status":"ok"}`。
3. 在工作台输入测试者自己的模型服务 API Key。
4. 先生成一个简单安装板，检查日志、SSE 进度、GLB 预览和文件下载。
5. 观察冷启动、峰值内存、单任务耗时和外部模型 API 失败率。

## 7. 正式多用户上线前仍需完成

- 将 `_JOBS` 从进程内存迁移至 Redis 或数据库。
- 将工程文件迁移至 TOS，并通过短期签名 URL 下载。
- 将生成 Python 的执行过程迁移至云沙箱。
- 增加用户、项目、配额、审计和任务归属控制。
- 去除公网 CDN 依赖，将 `model-viewer` 静态资源纳入受控发布包。
