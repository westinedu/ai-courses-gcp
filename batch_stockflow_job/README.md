# 批量编排 Cloud Run Job (Batch Orchestrator)

这是一个 Cloud Run Job，作为整个图卡生成流程的中央编排器。它负责按顺序、高效地调度三个数据准备引擎（财报、交易、新闻），并在所有数据准备就绪后，批量触发 QA 引擎生成最终的图卡。

## 功能

- **配置驱动**: 通过 GCS 上的 JSON 文件动态读取需要处理的股票列表 (`ticker_list.json`) 和图卡类型列表 (`card_types.json`)。
- **并发处理**: 异步并发调用三个数据引擎，最大化数据准备阶段的效率。
- **健壮的工作流**: 只有当所有数据引擎都成功完成后，才会进入图卡生成阶段。任何一个引擎失败都会导致 Job 失败，防止数据不一致。
- **服务间认证**: 自动处理 Cloud Run 服务之间的 OIDC 认证，确保安全的内部通信。
- **批量生成**: 高效地为所有股票和所有图卡类型批量生成图卡。

## 前提条件

1.  **GCP 项目**: 拥有一个启用了 Billing 的 GCP 项目。
2.  **已部署的服务**: 财报、交易、新闻和 QA 四个引擎已经作为 Cloud Run 服务成功部署，并且您可以获取到它们的 URL。
3.  **gcloud CLI**: 已安装并配置好 Google Cloud SDK。
4.  **权限**:
    *   部署 Job 的用户需要 `Cloud Run Admin`, `Service Account User`, `Storage Admin` 角色。
    *   创建一个专门用于运行此 Job 的**服务账号 (Service Account)**，例如 `job-runner-sa`。
    *   为此服务账号授予以下权限：
        *   `Cloud Run Invoker`: 允许它调用其他四个 Cloud Run 服务。
        *   `Storage Object Viewer`: 允许它从 GCS 读取配置文件。

## 配置文件

在您的 GCS 存储桶中，需要放置以下两个配置文件：

-   `gs://<your-bucket-name>/config/ticker_list.json`: 一个包含股票代码字符串的 JSON 数组。
-   `gs://<your-bucket-name>/config/card_types.json`: 一个包含图卡类型字符串的 JSON 数组。

## 部署流程

1.  **克隆仓库**: 将此 `batch-orchestrator-job` 目录克隆到本地。
2.  **配置部署参数**:
    -   直接传入项目 ID：`./deploy_batch_job.sh YOUR_PROJECT_ID`
    -   或通过环境变量：`PROJECT_ID=YOUR_PROJECT_ID ./deploy_batch_job.sh`
    -   如需切换到新项目，请同步更新四个引擎的 Cloud Run URL（`FINANCIAL_ENGINE_URL`/`TRADING_ENGINE_URL`/`NEWS_ENGINE_URL`/`QA_ENGINE_URL`）。
3.  **执行部署**: 在终端中，运行脚本：
    ```bash
    chmod +x deploy_batch_job.sh
    ./deploy_batch_job.sh YOUR_PROJECT_ID
    ```
    脚本将自动完成 Docker 镜像的构建、推送到 Artifact Registry 以及 Cloud Run Job 的部署。

## 调度 Job (Cloud Scheduler)

部署成功后，您可以设置一个 Cloud Scheduler 作业来定时触发此 Cloud Run Job。

1.  访问 Google Cloud Console 中的 **Cloud Scheduler** 页面。
2.  点击 **"创建作业"**。
3.  **目标类型**: 选择 **"Cloud Run"**。
4.  **作业**: 选择您刚刚部署的 `batch-orchestrator` 作业。
5.  **频率**: 使用 unix-cron 格式定义您的调度频率（例如，`0 5 * * *` 表示每天早上 5 点）。
6.  **时区**: 选择您的时区。
7.  点击 **"创建"**。

现在，Cloud Scheduler 将会按照您设定的频率，自动执行这个编排 Job，完成整个批量处理流程。

## 手动执行

要立即测试或手动触发 Job，请使用以下命令：

```bash
gcloud run jobs execute <JOB_NAME> --region <GCP_REGION>

card_targets.json  是作为ticker与card type之间的mapping
