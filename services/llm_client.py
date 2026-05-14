import json
import openai
from openai import AsyncOpenAI
from config.config import config

class LLMClient:
    def __init__(self, base_url: str, api_key: str, model_name: str, vision_enabled: bool = False, tools_enabled: bool = False):
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self.model_name = model_name
        self.vision_enabled = vision_enabled
        self.tools_enabled = tools_enabled

    async def generate_response(self, messages: list, temperature: float = 0.7, tools: list = None) -> dict:
        try:
            active_tools = tools if self.tools_enabled and tools else None

            response = await self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=temperature,
                tools=active_tools
            )
            
            response_message = response.choices[0].message

            if response_message.tool_calls:
                return {
                    "type": "tool_calls",
                    "tool_calls": response_message.tool_calls,
                    "message_obj": response_message
                }
                
            return {
                "type": "text",
                "content": response_message.content
            }
            
        except openai.BadRequestError as e:
            return {
                "type": "error",
                "content": f"[Системное уведомление] Ошибка API. Модель не поддерживает текущий формат запроса.\nДетали: {str(e)}"
            }
        except Exception as e:
            return {
                "type": "error",
                "content": f"[Системное уведомление] Внутренняя ошибка генерации ответа: {str(e)}"
            }

llm = LLMClient(
    base_url=config.base_url,
    api_key=config.api_key,
    model_name=config.model_name,
    vision_enabled=config.vision_enabled,
    tools_enabled=config.tools_enabled
)