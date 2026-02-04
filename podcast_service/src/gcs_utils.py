#!/usr/bin/env python3
"""
Google Cloud Storage 工具函数
用于将生成的脚本和音频上传到指定的 GCS 存储桶中
支持 IAM 代签方式生成 Signed URL（推荐用于 Cloud Run）
"""

from __future__ import annotations

import os
import logging
import requests
import datetime
from pathlib import Path
from typing import Optional
from datetime import timedelta

import google.auth
from google.auth.transport.requests import Request as AuthRequest
from google.cloud import storage
from google.cloud.storage import Bucket

logger = logging.getLogger(__name__)


class GCSUploader:
    """简单的 GCS 上传器（单例形式复用 storage client）"""

    _client: Optional[storage.Client] = None

    @classmethod
    def _get_client(cls) -> storage.Client:
        if cls._client is None:
            cls._client = storage.Client()
        return cls._client

    @classmethod
    def upload_file(
        cls,
        local_path: Path,
        bucket_name: str,
        destination_path: str,
    ) -> str:
        """
        将本地文件上传到 GCS

        Args:
            local_path: 需要上传的本地文件路径
            bucket_name: 目标 GCS 存储桶名称
            destination_path: 上传后的对象路径（例如 generated_scripts/foo.json）

        Returns:
            上传后的 gs:// URI
        """
        if not bucket_name:
            raise ValueError("bucket_name 不能为空")

        if not local_path.exists():
            raise FileNotFoundError(f"待上传的文件不存在: {local_path}")

        client = cls._get_client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(destination_path)

        logger.info(f"☁️  正在上传到 GCS: gs://{bucket_name}/{destination_path}")
        blob.upload_from_filename(str(local_path))
        logger.info("✅ 上传完成")

        return f"gs://{bucket_name}/{destination_path}"

    @classmethod
    def generate_signed_url(
        cls,
        bucket_name: str,
        blob_name: str,
        expiration_hours: int = 24,
    ) -> str:
        """
        生成一个 IAM 代签的 Signed URL（推荐用于 Cloud Run）。
        
        该方法使用当前服务账号的权限来代签 URL，无需本地存储私钥。
        这是在 Google Cloud Run 上生成签名 URL 的最佳实践。
        
        必需的 IAM 权限：
        - roles/iam.serviceAccountTokenCreator（服务账号对自己）
        - 或 roles/iam.serviceAccountUser
        
        Args:
            bucket_name: GCS 存储桶名称
            blob_name: 对象路径
            expiration_hours: URL 有效期（小时）。默认24小时。
        
        Returns:
            可下载的签名 URL
            
        Raises:
            ValueError: 如果参数无效
            RuntimeError: 如果无法获取服务账号信息或权限不足
        """
        if not bucket_name:
            raise ValueError("bucket_name 不能为空")
        
        # 将小时转换为分钟，用于 timedelta
        expiration_minutes = expiration_hours * 60
        
        try:
            client = cls._get_client()
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            
            # 1. 获取当前环境的凭证（包含 cloud-platform 权限）
            creds, project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            
            # 2. 如果凭证不可用，则刷新
            auth_req = AuthRequest(session=requests.Session())
            if not creds.valid:
                creds.refresh(auth_req)
            
            # 3. 获取当前服务账号的邮箱
            sa_email = cls._get_service_account_email()
            
            # 4. 使用 IAM 代签（service_account_email + access_token）生成 Signed URL（v4）
            #    注意：不能直接传 credentials=compute_engine.Credentials，
            #    否则库会尝试用本地私钥签名而报 "you need a private key to sign"。
            logger.info(
                f"🔏 正在使用服务账号签名: {sa_email} (access_token present={bool(creds.token)})"
            )
            signed_url = blob.generate_signed_url(
                version="v4",
                expiration=datetime.timedelta(minutes=expiration_minutes),
                method="GET",
                service_account_email=sa_email,
                access_token=creds.token,  # 让库调用 IAM Credentials API 代签
            )
            
            logger.info(f"✅ 生成签名 URL ({expiration_hours}小时有效期): {blob_name}")
            return signed_url
            
        except Exception as e:
            error_msg = str(e)
            if "PERMISSION_DENIED" in error_msg or "403" in error_msg:
                logger.error(
                    f"❌ IAM 权限不足。请运行以下命令添加权限：\n"
                    f"gcloud iam service-accounts add-iam-policy-binding {sa_email} \\\n"
                    f"  --member='serviceAccount:{sa_email}' \\\n"
                    f"  --role='roles/iam.serviceAccountTokenCreator'"
                )
                # 常见误配：容器设置了 GOOGLE_APPLICATION_CREDENTIALS，导致以“密钥文件账号”调用 signBlob
                if os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
                    logger.error(
                        "⚠️ 检测到 GOOGLE_APPLICATION_CREDENTIALS 已设置。Cloud Run 上建议删除该变量，"
                        "改为使用运行时服务账号进行 IAM 代签（Workload Identity）。"
                    )
            logger.error(f"❌ 生成签名 URL 失败: {e}")
            raise RuntimeError(f"无法生成签名 URL，请检查 IAM 权限: {e}") from e
    
    @classmethod
    def _get_service_account_email(cls) -> str:
        """
        获取当前运行环境的服务账号邮箱。
        
        优先级：
        1. 环境变量 GOOGLE_SERVICE_ACCOUNT_EMAIL
        2. 元数据服务器（Cloud Run/GKE）
        3. 从当前凭证中提取
        
        Returns:
            服务账号邮箱
            
        Raises:
            RuntimeError: 如果无法获取
        """
        import os
        
        # 1. 检查环境变量
        sa_email = os.getenv("GOOGLE_SERVICE_ACCOUNT_EMAIL")
        if sa_email:
            logger.info(f"📧 从环境变量获得服务账号: {sa_email}")
            return sa_email
        
        # 2. 尝试从元数据服务器获取（Cloud Run/GKE）
        try:
            response = requests.get(
                "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
                headers={"Metadata-Flavor": "Google"},
                timeout=2,
            )
            if response.ok:
                sa_email = response.text.strip()
                logger.info(f"📧 从元数据服务器获得服务账号: {sa_email}")
                return sa_email
        except Exception as e:
            logger.debug(f"⚠️  无法从元数据服务器获得服务账号: {e}")
        
        # 3. 最后尝试从当前凭证中提取
        try:
            creds, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
            if hasattr(creds, 'service_account_email'):
                sa_email = creds.service_account_email
                logger.info(f"📧 从凭证中获得服务账号: {sa_email}")
                return sa_email
        except Exception as e:
            logger.debug(f"⚠️  无法从凭证中获得服务账号: {e}")
        
        raise RuntimeError(
            "无法获取服务账号邮箱。请设置环境变量 GOOGLE_SERVICE_ACCOUNT_EMAIL，"
            "或确保运行在 Google Cloud（Cloud Run/GKE）环境中。"
        )

