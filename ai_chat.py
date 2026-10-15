import os
import re
import json
import asyncio
import logging
import urllib.request
import urllib.parse
from typing import Optional, List
from dotenv import load_dotenv

import database

load_dotenv()
logger = logging.getLogger("AIChat")

AI_SYSTEM_INSTRUCTION = """
你是 Discord 群組裡的成員 picture bro。你在群組裡跟人聊天。

【發言風格要求】：
1. 單純對話、直接回答：
   - 說話乾脆自然，像平時打字聊天一樣，直接回答核心內容即可。
   - 嚴禁使用「安安」、「安啦」、「嗨囉」、「Yo」、「啦～」、「捏」、「唄」等任何奇怪刻意的語助詞或套近乎開場白。單純對話就好。
   - 當對方說「你好」時，簡單回「你好」打招呼即可，不要擅自加油添醋或多問額外問題。
2. 網路迷因與次文化梗理解：
   - 遇到網路用語、動漫迷因、時事梗、諧音或社群黑話時，請從網路幽默與次文化角度理解其真正含義，切勿照字面死板解釋。
3. 自主判斷是否聯網搜尋：
   - 當群友提到你不確定、不知道、或是最新爆紅的網路新梗、名詞、動漫或時事時，請主動呼叫 `search_web` 工具搜尋，查明含義後再以日常口氣接話。
   - 一般日常閒聊、常識或打招呼時，請直接回答，不要呼叫搜尋工具。
4. 不用刻意注重標點符號：
   - 像平常打字隨性發送即可，不必每句都嚴格加上標準標點符號。
5. 極致簡短：
   - 通常 1 到 2 句即可，除非對方明確要求詳細解釋，否則絕不超過 3 行。
6. 語言與文明：
   - 主要使用繁體中文。
   - 嚴禁粗俗髒話：一律不可罵髒話、不可出現粗俗不雅字詞（如幹、靠、三小、操等粗口）。
   - 切勿使用機器人客服腔（如「很高興為您服務」、「您好！請問有什麼我可以幫您」等官腔）。
7. 上下文配合：
   - 參考前後文紀錄正常接話。
"""

SEARCH_TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "search_web",
        "description": "搜尋網路以獲取最新時事、不認識的網路流行梗、動漫名詞、縮寫或特定名詞解釋。只有在你不確定、不知道或需要查證時才呼叫。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "精準的搜尋關鍵字，例如「黑白大廚 梗」、「五條悟 2.5條」"
                }
            },
            "required": ["query"]
        }
    }
}


def _fetch_wiki_sync(query: str) -> Optional[str]:
    """同步抓取百科/網路摘要 (執行於獨立工作線程中，不阻礙主迴圈)"""
    # 智能清理贅詞以提升檢索命中率
    clean_q = re.sub(r'^(請問|問一下|你知道|什麼是|誰是|介紹一下|\?|？|梗|是什麼梗|是什麼意思|\s)+', '', query)
    clean_q = re.sub(r'(是什麼梗|是什麼意思|的梗|是什麼|\?|？|\s)+$', '', clean_q).strip()
    if not clean_q:
        clean_q = query.strip()

    url = f"https://zh.wikipedia.org/w/api.php?action=query&list=search&srsearch={urllib.parse.quote(clean_q)}&format=json&utf8=1"
    req = urllib.request.Request(url, headers={"User-Agent": "DiscordMemeBot/1.0 (https://github.com)"})
    try:
        with urllib.request.urlopen(req, timeout=3.5) as r:
            data = json.loads(r.read())
            items = data.get("query", {}).get("search", [])
            if items:
                title = items[0]["title"]
                snippet = re.sub(r'<.*?>', '', items[0]["snippet"])
                return f"{title}: {snippet}"
    except Exception:
        pass
    return None


class AIChatManager:
    def __init__(self):
        self.groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model_name = os.getenv("AI_MODEL") or os.getenv("GROQ_MODEL") or "qwen/qwen3.8-27b"
        self.allowed_guild_id = os.getenv("GUILD_ID", "").strip()
        self.allowed_channel_id = os.getenv("AI_CHANNEL_ID", "").strip()
        self._groq_client = None
        self._gemini_client = None

    def reload_config(self):
        """重新讀取環境變數配置"""
        load_dotenv(override=True)
        self.groq_api_key = os.getenv("GROQ_API_KEY", "").strip()
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.model_name = os.getenv("AI_MODEL") or os.getenv("GROQ_MODEL") or "qwen/qwen3.8-27b"
        self.allowed_guild_id = os.getenv("GUILD_ID", "").strip()
        self.allowed_channel_id = os.getenv("AI_CHANNEL_ID", "").strip()
        self._groq_client = None
        self._gemini_client = None

    def is_allowed_scope(self, guild_id: Optional[int], channel_id: Optional[int]) -> bool:
        """檢查是否在允許的伺服器與頻道 (依環境變數限制)"""
        if self.allowed_guild_id and str(guild_id) != self.allowed_guild_id:
            return False
        if self.allowed_channel_id and str(channel_id) != self.allowed_channel_id:
            return False
        return True

    def get_groq_client(self):
        """取得或建立 Groq 客戶端"""
        if self._groq_client is not None:
            return self._groq_client
        if not self.groq_api_key:
            return None
        try:
            from groq import AsyncGroq
            self._groq_client = AsyncGroq(api_key=self.groq_api_key)
            logger.info(f"✅ Groq API 客戶端已就緒 (配置模型: {self.model_name})")
            return self._groq_client
        except Exception as e:
            logger.error(f"[AI-錯誤] 初始化 Groq 客戶端失敗: {e}")
            return None

    def get_gemini_client(self):
        """取得或建立 Gemini 備用客戶端"""
        if self._gemini_client is not None:
            return self._gemini_client
        if not self.gemini_api_key:
            return None
        try:
            from google import genai
            self._gemini_client = genai.Client(api_key=self.gemini_api_key)
            logger.info("✅ Gemini API 備用客戶端已就緒")
            return self._gemini_client
        except Exception as e:
            logger.error(f"[AI-錯誤] 初始化 Gemini 客戶端失敗: {e}")
            return None

    async def _generate_groq(
        self,
        history_records: List[dict],
        user_name: str,
        user_prompt: str
    ) -> Optional[str]:
        """使用 Groq API 進行推理，支援 AI 自主決定是否呼叫搜尋工具"""
        client = self.get_groq_client()
        if not client:
            return None

        # 組裝 OpenAI / Groq 格式的 Messages
        messages = [{"role": "system", "content": AI_SYSTEM_INSTRUCTION}]
        for record in history_records:
            role = "user" if record["role"] == "user" else "assistant"
            sender = record.get("user_name") or "群友"
            content = f"[{sender}]: {record['content']}" if role == "user" else record["content"]
            messages.append({"role": role, "content": content})

        messages.append({
            "role": "user",
            "content": f"[{user_name}]: {user_prompt.strip()}"
        })

        candidate_models = [self.model_name, "qwen/qwen3.8-27b", "openai/gpt-oss-120b"]
        seen = set()
        models = [m for m in candidate_models if m and not (m in seen or seen.add(m))]

        for cur_model in models:
            try:
                logger.info(f"[AI-生成] 正在向 Groq ({cur_model}) 請求回覆 (交由 AI 自主判斷是否聯網)...")
                
                # 第一輪：提供 search_web 工具，由 AI 模型自主判斷是否需要搜尋
                chat_completion = await client.chat.completions.create(
                    model=cur_model,
                    messages=messages,
                    tools=[SEARCH_TOOL_DEFINITION],
                    tool_choice="auto",
                    temperature=0.6,
                    max_tokens=300
                )
                choice_msg = chat_completion.choices[0].message

                # 判斷 AI 是否自主決定呼叫搜尋工具 (支援標準 tool_calls 與模型內部 XML 標籤)
                search_kw = None
                if choice_msg.tool_calls:
                    try:
                        args = json.loads(choice_msg.tool_calls[0].function.arguments)
                        search_kw = args.get("query", user_prompt).strip()
                    except Exception:
                        search_kw = user_prompt.strip()
                elif choice_msg.content and "<tool_call>" in choice_msg.content:
                    m = re.search(r'<parameter=query>\s*(.*?)\s*</parameter>', choice_msg.content, re.DOTALL)
                    if m:
                        search_kw = m.group(1).strip("\"'")

                if search_kw:
                    logger.info(f"🌐 [AI-自主判斷] AI 決定聯網搜尋關鍵字: '{search_kw}'")
                    search_res = await asyncio.to_thread(_fetch_wiki_sync, search_kw)
                    search_content = search_res if search_res else f"網路上暫無關於「{search_kw}」的詳細詞條"

                    clean_messages = list(messages)
                    clean_messages.append({
                        "role": "system",
                        "content": f"【聯網檢索資訊】：{search_content}\n請根據以上搜尋結果，以繁體中文直接回答用戶，絕不輸出任何標籤。"
                    })
                    final_completion = await client.chat.completions.create(
                        model=cur_model,
                        messages=clean_messages,
                        temperature=0.6,
                        max_tokens=300
                    )
                    text = final_completion.choices[0].message.content
                else:
                    logger.info("[AI-自主判斷] AI 判斷無需聯網，直接回答。")
                    text = choice_msg.content

                if text:
                    # 清理任何殘留的模型內部標籤
                    text = re.sub(r'<tool_call>.*?</tool_call>', '', text, flags=re.DOTALL).strip()

                if text and text.strip():
                    if self.model_name != cur_model:
                        logger.info(f"💡 成功切換並記住有效 Groq 模型: {cur_model}")
                        self.model_name = cur_model
                    return text.strip()

            except Exception as e:
                err_msg = str(e)
                if "404" in err_msg or "model_not_found" in err_msg:
                    logger.warning(f"[AI-Groq 模型不存在] 模型 {cur_model} 在 Groq 已下架或無權限 (404)，嘗試切換下一個可用模型...")
                    continue
                else:
                    logger.error(f"[AI-Groq 異常 ({cur_model})]: {e}")
                    break
        return None

    async def _generate_gemini(
        self,
        history_records: List[dict],
        user_name: str,
        user_prompt: str
    ) -> Optional[str]:
        """Gemini 備用生成"""
        client = self.get_gemini_client()
        if not client:
            return None

        from google.genai import types
        contents = []
        for record in history_records:
            role = "user" if record["role"] == "user" else "model"
            sender = record.get("user_name") or "群友"
            msg_text = f"[{sender}]: {record['content']}" if role == "user" else record["content"]
            contents.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=msg_text)]
                )
            )
        
        user_msg = f"[{user_name}]: {user_prompt.strip()}"
        contents.append(
            types.Content(
                role="user",
                parts=[types.Part.from_text(text=user_msg)]
            )
        )

        cfg = types.GenerateContentConfig(
            system_instruction=AI_SYSTEM_INSTRUCTION,
            temperature=0.6,
            max_output_tokens=300
        )
        logger.info(f"[AI-生成] 正在向 Gemini 備用請求回覆 (上下文: {len(history_records)} 則)...")
        resp = await client.aio.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=contents,
            config=cfg
        )
        return resp.text.strip() if resp.text else None

    async def generate_response(
        self,
        guild_id: int,
        channel_id: int,
        user_id: int,
        user_name: str,
        user_prompt: str
    ) -> str:
        """非同步生成 AI 回覆：【AI自主聯網搜尋判斷】+【Groq極速推理】"""
        # 1. 讀取該頻道本地保存的最近 50 則歷史上下文
        history_records = database.get_chat_history(str(channel_id), limit=50)

        reply_text = None

        # 2. 優先嘗試 Groq (交由模型自主呼叫 search_web)
        if self.groq_api_key:
            try:
                reply_text = await self._generate_groq(history_records, user_name, user_prompt)
            except Exception as e:
                logger.error(f"[AI-Groq 異常]: {e}")

        # 3. 若未填寫 GROQ_API_KEY 或 Groq 調用失敗，嘗試 Gemini 備用
        if not reply_text and self.gemini_api_key:
            if not self.groq_api_key:
                logger.info("ℹ️ 未在 .env 設定 GROQ_API_KEY，暫時由 Gemini (gemini-3.5-flash-lite) 支援。")
            try:
                reply_text = await self._generate_gemini(history_records, user_name, user_prompt)
            except Exception as e:
                logger.error(f"[AI-Gemini 備用異常]: {e}")

        # 5. 若皆無可用 Key
        if not reply_text:
            if not self.groq_api_key and not self.gemini_api_key:
                return "（請先在 .env 中填寫 GROQ_API_KEY，可至 https://console.groq.com/keys 免費領取！）"
            return "笑死，我腦袋剛剛突然斷線了，等會再跟我聊！"

        # 6. 成功取得回覆後，寫入 SQLite 本地對話紀錄 (保持最新 50 筆)
        try:
            database.add_chat_message(
                guild_id=str(guild_id),
                channel_id=str(channel_id),
                user_id=str(user_id),
                user_name=user_name,
                role="user",
                content=user_prompt.strip()
            )
            database.add_chat_message(
                guild_id=str(guild_id),
                channel_id=str(channel_id),
                user_id="ai",
                user_name="picture bro",
                role="model",
                content=reply_text
            )
        except Exception as db_err:
            logger.error(f"[AI-資料庫寫入異常]: {db_err}")

        logger.info(f"[AI-生成完成] 回覆: '{reply_text[:60]}...'")
        return reply_text


# 全域單例物件
ai_manager = AIChatManager()
