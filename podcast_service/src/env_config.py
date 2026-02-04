"""
环境变量加载和验证模块
自动从 .env 文件加载配置，并进行验证
"""

import os
import sys
import logging
from pathlib import Path
from typing import Optional, Dict, Any
from dotenv import load_dotenv, find_dotenv

# 设置日志
logger = logging.getLogger(__name__)


class EnvConfig:
    """环境变量配置类 - 自动加载和管理所有环境变量"""

    # 必需的环境变量
    REQUIRED_VARS = [
        'OPENAI_API_KEY',
    ]

    # 可选的环境变量及其默认值
    OPTIONAL_VARS = {
        'API_HOST': '0.0.0.0',
        'API_PORT': '8080',
        'LOG_LEVEL': 'INFO',
        'LOG_FILE': 'logs/api.log',
        'LLM_MODEL': 'gpt-4o-mini',
        'LLM_TEMPERATURE': '0.7',
        'LLM_MAX_TOKENS': '4000',
        'TTS_ENGINE': 'google-cloud',  # 使用 Google Cloud TTS (已验证质量)
        'TTS_VOICE_ID': 'default',
        'DATA_DIR': 'data',
        'SCRIPTS_DIR': 'data/generated_scripts',
        'PODCASTS_DIR': 'data/generated_podcasts',
        'CACHE_DIR': 'data/cache',
        'MAX_CONCURRENT_REQUESTS': '5',
        'REQUEST_TIMEOUT': '300',
        'DEBUG': 'false',
        'ENVIRONMENT': 'production',
        'GCS_BUCKET_NAME': '',
    }

    def __init__(self, env_file: Optional[str] = None, auto_create: bool = True):
        """
        初始化环境配置
        
        Args:
            env_file: .env 文件路径，如果为 None 将自动搜索
            auto_create: 是否自动在项目根目录创建 .env 文件
        """
        self._env_file = env_file or self._find_env_file()
        self._config: Dict[str, Any] = {}
        self._load_config(auto_create)

    @staticmethod
    def _find_env_file() -> str:
        """自动查找 .env 文件"""
        # 按优先级搜索
        search_paths = [
            '.env',  # 当前目录
            Path.cwd() / '.env',  # 工作目录
            Path(__file__).parent / '.env',  # 脚本所在目录
            Path(__file__).parent.parent / '.env',  # 上一级目录
        ]

        for path in search_paths:
            path = Path(path)
            if path.exists():
                logger.info(f"✅ 找到 .env 文件: {path.absolute()}")
                return str(path)

        logger.warning("⚠️  未找到 .env 文件，将使用系统环境变量")
        return None

    def _load_config(self, auto_create: bool = True):
        """加载和验证配置"""
        # 第一步：加载 .env 文件
        if self._env_file:
            load_dotenv(self._env_file, override=False)
            logger.info(f"📄 已加载 .env 文件: {self._env_file}")
        else:
            # 尝试查找 .env 文件
            dotenv_path = find_dotenv()
            if dotenv_path:
                load_dotenv(dotenv_path, override=False)
                logger.info(f"📄 已加载 .env 文件: {dotenv_path}")
            elif auto_create:
                logger.warning("⚠️  未找到 .env 文件，将自动创建...")
                self._create_default_env()
                load_dotenv('.env', override=False)
            else:
                logger.warning("⚠️  未找到 .env 文件，使用系统环境变量")

        # 第二步：验证必需变量
        self._validate_required_vars()

        # 第三步：加载所有变量
        self._load_all_vars()

    def _create_default_env(self):
        """创建默认的 .env 文件"""
        try:
            env_path = Path('.env')
            
            # 生成内容
            content = "# 播客引擎 v4 - 环境配置\n"
            content += "# ⚠️  请设置 OPENAI_API_KEY\n\n"
            
            for key, default_value in self.OPTIONAL_VARS.items():
                if default_value == '':
                    content += f"# {key}=your-value-here\n"
                else:
                    content += f"{key}={default_value}\n"
            
            # 添加必需变量（未设置）
            content += "\n# 必需配置（必须设置）\n"
            for var in self.REQUIRED_VARS:
                content += f"# {var}=your-actual-value-here\n"
            
            env_path.write_text(content)
            logger.info(f"✅ 已创建 .env 文件: {env_path.absolute()}")
            logger.warning("⚠️  请在 .env 文件中设置 OPENAI_API_KEY")
            
        except Exception as e:
            logger.error(f"❌ 创建 .env 文件失败: {e}")

    def _validate_required_vars(self):
        """验证必需的环境变量是否已设置"""
        missing = []
        
        for var in self.REQUIRED_VARS:
            if not os.getenv(var):
                missing.append(var)
        
        if missing:
            error_msg = f"❌ 缺少必需的环境变量: {', '.join(missing)}"
            logger.error(error_msg)
            logger.info("💡 解决方案:")
            logger.info("1. 创建 .env 文件")
            logger.info("2. 添加: OPENAI_API_KEY=your-key-here")
            logger.info("3. 或在终端设置: export OPENAI_API_KEY=your-key-here")
            raise ValueError(error_msg)

    def _load_all_vars(self):
        """加载所有环境变量"""
        # 加载必需变量
        for var in self.REQUIRED_VARS:
            value = os.getenv(var)
            if value:
                # 对于 API Key，只显示前缀
                display_value = value[:10] + '...' if len(value) > 10 else value
                logger.info(f"✅ {var}: {display_value}")
            self._config[var] = value

        # 加载可选变量
        for var, default in self.OPTIONAL_VARS.items():
            value = os.getenv(var, default)
            self._config[var] = value
            if os.getenv(var):  # 只在用户自定义时显示
                logger.debug(f"📌 {var}: {value}")

    def get(self, key: str, default: Optional[Any] = None) -> Any:
        """获取配置值"""
        return self._config.get(key, default)

    def __getitem__(self, key: str) -> Any:
        """支持 [] 访问"""
        return self._config[key]

    def __contains__(self, key: str) -> bool:
        """支持 in 操作符"""
        return key in self._config

    @property
    def openai_api_key(self) -> str:
        """获取 OpenAI API Key"""
        return self.get('OPENAI_API_KEY')

    @property
    def api_host(self) -> str:
        """获取 API 主机"""
        return self.get('API_HOST', '0.0.0.0')

    @property
    def api_port(self) -> int:
        """获取 API 端口"""
        return int(self.get('API_PORT', 8080))

    @property
    def log_level(self) -> str:
        """获取日志级别"""
        return self.get('LOG_LEVEL', 'INFO')

    @property
    def llm_model(self) -> str:
        """获取 LLM 模型名称"""
        return self.get('LLM_MODEL', 'gpt-4o-mini')

    @property
    def debug(self) -> bool:
        """获取调试模式"""
        return self.get('DEBUG', 'false').lower() in ('true', '1', 'yes')

    @property
    def gcs_bucket_name(self) -> str:
        """获取 GCS 存储桶名称（可为空）"""
        return self.get('GCS_BUCKET_NAME', '').strip()

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return dict(self._config)

    def print_config(self, hide_secrets: bool = True):
        """打印配置（可选隐藏敏感信息）"""
        print("\n📋 环境配置:")
        print("=" * 50)
        for key, value in self._config.items():
            if hide_secrets and 'KEY' in key:
                display_value = str(value)[:10] + '...' if value else 'Not set'
            else:
                display_value = value
            print(f"  {key}: {display_value}")
        print("=" * 50 + "\n")


# 全局配置实例
_config_instance: Optional[EnvConfig] = None


def load_env(env_file: Optional[str] = None, auto_create: bool = True) -> EnvConfig:
    """
    加载环境配置（推荐在应用启动时调用）
    
    Args:
        env_file: .env 文件路径
        auto_create: 是否自动创建 .env 文件
    
    Returns:
        EnvConfig 实例
    
    Example:
        config = load_env()
        api_key = config.openai_api_key
    """
    global _config_instance
    
    if _config_instance is None:
        _config_instance = EnvConfig(env_file=env_file, auto_create=auto_create)
    
    return _config_instance


def get_config() -> EnvConfig:
    """获取全局配置实例"""
    global _config_instance
    
    if _config_instance is None:
        raise RuntimeError(
            "❌ 配置未初始化。请先调用 load_env() 或在应用启动时调用。"
        )
    
    return _config_instance


if __name__ == '__main__':
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 加载配置
    config = load_env()
    
    # 打印配置
    config.print_config()
