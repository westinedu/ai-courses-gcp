#!/usr/bin/env python3
"""
从新闻文件生成播客脚本的工具
使用方式：
    python generate_from_news.py <news_file_path>
"""

import sys
import json
import requests

def read_news_file(file_path):
    """读取新闻文件"""
    with open(file_path, 'r', encoding='utf-8') as f:
        return f.read()

def generate_podcast_from_news(news_content, topic, duration_minutes=5, generate_audio=False):
    """
    从新闻内容生成播客
    
    Args:
        news_content: 新闻内容文本
        topic: 播客主题
        duration_minutes: 目标时长（分钟）
        generate_audio: 是否生成音频
    """
    
    # API 配置
    api_url = "http://127.0.0.1:8080/v4/generate"
    
    # 构建请求数据
    payload = {
        "topic": topic,
        "style_name": "english_4_panel",
        "tone": "professional",
        "dialogue_style": "conversation",
        "duration_minutes": duration_minutes,
        "generate_audio": generate_audio,
        "source_content": news_content  # 关键：传入真实新闻内容
    }
    
    print(f"\n🎙️ 正在生成播客...")
    print(f"   主题: {topic}")
    print(f"   目标时长: {duration_minutes} 分钟")
    print(f"   源内容长度: {len(news_content)} 字符")
    print(f"   生成音频: {'是' if generate_audio else '否'}")
    
    # 发送请求
    try:
        response = requests.post(api_url, json=payload)
        response.raise_for_status()
        
        result = response.json()
        
        if result.get('status') == 'success':
            print(f"\n✅ 播客生成成功！")
            print(f"   脚本文件: {result.get('script_file')}")
            print(f"   段落数: {result['script_preview']['num_segments']}")
            print(f"   预计时长: {result['script_preview']['estimated_duration_seconds']:.1f} 秒")
            
            if result.get('audio_file'):
                print(f"   音频文件: {result['audio_file']}")
            
            print(f"\n📝 脚本预览:")
            print(f"   标题: {result['script_preview']['title']}")
            print(f"   第一段: {result['script_preview']['first_segment']['text'][:150]}...")
            
            return result
        else:
            print(f"\n❌ 生成失败: {result.get('message')}")
            return None
            
    except requests.exceptions.RequestException as e:
        print(f"\n❌ API 请求失败: {e}")
        return None

def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("使用方式: python generate_from_news.py <news_file_path> [topic] [duration_minutes]")
        print("\n示例:")
        print("  python generate_from_news.py news.txt")
        print("  python generate_from_news.py news.txt '加密货币市场分析' 7")
        sys.exit(1)
    
    # 读取参数
    news_file = sys.argv[1]
    topic = sys.argv[2] if len(sys.argv) > 2 else "加密货币市场最新动态分析"
    duration_minutes = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    
    # 读取新闻内容
    try:
        news_content = read_news_file(news_file)
        print(f"✅ 成功读取新闻文件: {news_file}")
        print(f"   内容长度: {len(news_content)} 字符")
        
        # 生成播客
        result = generate_podcast_from_news(
            news_content=news_content,
            topic=topic,
            duration_minutes=duration_minutes,
            generate_audio=False  # 默认不生成音频，只生成脚本
        )
        
        if result:
            print(f"\n🎉 完成！脚本已保存。")
            print(f"\n如需生成音频，请运行:")
            print(f"  python generate_from_news.py {news_file} '{topic}' {duration_minutes} --audio")
        
    except FileNotFoundError:
        print(f"❌ 文件不存在: {news_file}")
        sys.exit(1)
    except Exception as e:
        print(f"❌ 发生错误: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
