import os
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()


class OpenAICompatibleClient:
    """兼容 OpenAI 接口的 LLM 客户端，支持单轮与多轮对话。"""

    def __init__(self, model: str = None, api_key: str = None, base_url: str = None):
        self.model = model or os.getenv("LLM_MODEL_ID")
        api_key = api_key or os.getenv("LLM_API_KEY")
        base_url = base_url or os.getenv("LLM_BASE_URL")
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def generate(self, prompt: str, system_prompt: str = None) -> str:
        """单轮对话：传入 prompt 和可选的 system_prompt，返回模型回复。"""
        print("正在调用大语言模型（单轮）...")
        try:
            messages = []
            if system_prompt:
                messages.append({'role': 'system', 'content': system_prompt})
            messages.append({'role': 'user', 'content': prompt})

            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False
            )
            answer = response.choices[0].message.content
            print("大语言模型响应成功。")
            return answer
        except Exception as e:
            print(f"调用LLM API时发生错误: {e}")
            raise  # 抛出异常，让上层统一处理

    def chat(self, messages: list) -> str:
        """
        多轮对话：传入完整的 messages 列表，返回模型回复。
        """
        print("正在调用大语言模型（多轮对话）...")
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                stream=False
            )
            answer = response.choices[0].message.content
            print("大语言模型响应成功。")
            return answer
        except Exception as e:
            print(f"调用LLM API时发生错误: {e}")
            raise  # 抛出异常，让上层统一处理

if __name__ == "__main__":
    client = OpenAICompatibleClient()
    print(client.generate("你好"))