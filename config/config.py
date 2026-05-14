import os
from dotenv import load_dotenv

class Config:
    def __init__(self):
        load_dotenv(override=True)
        self.bot_token: str = os.getenv("BOT_TOKEN", "")
        self.admin_id: int = int(os.getenv("ADMIN_ID", 0))
        
        self.use_openrouter: bool = os.getenv("USE_OPENROUTER", "False").lower() == "true"
        
        if self.use_openrouter:
            self.base_url = "https://openrouter.ai/api/v1"
            self.api_key = os.getenv("OPENROUTER_API_KEY", "")
            self.model_name = os.getenv("OPENROUTER_MODEL", "openai/gpt-oss-120b:free")
        else:
            self.base_url = os.getenv("LM_STUDIO_URL", "http://127.0.0.1:1234/v1")
            self.api_key = "lm-studio"
            self.model_name = "local-model"

        self.vision_enabled: bool = os.getenv("VISION_ENABLED", "False").lower() == "true"
        self.tools_enabled: bool = os.getenv("TOOLS_ENABLED", "False").lower() == "true"
        self.web_search_enabled: bool = os.getenv("WEB_SEARCH_ENABLED", "False").lower() == "true"
        self.tavily_api_key: str = os.getenv("TAVILY_API_KEY", "")

config = Config()