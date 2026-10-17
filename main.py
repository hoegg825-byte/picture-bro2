import os
import sys
import io
import re
import mimetypes
import logging
import asyncio
import aiohttp
import discord
from discord.ext import commands
from discord import app_commands, ui
from dotenv import load_dotenv

# 強制在 Windows 控制台啟用 UTF-8 編碼輸出
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

import database
from gdrive import drive_client
from ai_chat import ai_manager

# ================= 載入環境變數 =================
load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
FOLDER_ID = os.getenv('GDRIVE_FOLDER_ID') or os.getenv('GOOGLE_DRIVE_FOLDER_ID')
DEV_GUILD_ID = os.getenv('GUILD_ID')

# 設定日誌
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("MemeBot")

# 遮蔽底層心跳與套件雜訊，確保終端機除錯訊息清晰
import warnings
warnings.filterwarnings("ignore", category=UserWarning, module="google_genai")
warnings.filterwarnings("ignore", category=FutureWarning, module="google.api_core")
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.client").setLevel(logging.INFO)
logging.getLogger("googleapiclient").setLevel(logging.WARNING)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


# ================= Discord Bot 初始化 =================
class MemeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # 監聽回覆訊息內容必需
        intents.messages = True
        intents.guilds = True

        super().__init__(
            command_prefix='!',
            intents=intents,
            help_command=None
        )

        # AI 聊天非同步隊列 (應對短時間多訊息，排隊依序處理防止並發衝突)
        self.ai_queue = asyncio.Queue()
        self.ai_worker_task = None

    async def ai_worker(self):
        """AI 對話獨立處理工人：單線程排隊依序處理，保證上下文順序並防止多則訊息競爭"""
        logger.info("🤖 AI 對話隊列工作緒已就緒 (支援排隊依序處理，防止短時間多訊息競爭)...")
        while True:
            message = await self.ai_queue.get()
            try:
                # 提取純對話文字 (移除 @機器人 mention)
                clean_prompt = re.sub(r'<@!?\d+>', '', message.content).strip()

                # 若為回覆其他訊息 (但無引號存圖標籤)，抓取被回覆訊息當作對話前文
                reply_context = ""
                if message.reference and message.reference.message_id:
                    try:
                        ref_msg = await message.channel.fetch_message(message.reference.message_id)
                        if ref_msg.content:
                            reply_context = f" (針對上一句「{ref_msg.author.display_name}: {ref_msg.content[:80]}」的回覆)"
                    except Exception:
                        pass

                full_prompt = f"{clean_prompt}{reply_context}" if reply_context else clean_prompt
                if not full_prompt:
                    full_prompt = "哈囉！"

                logger.info(f"[除錯-AI處理] 正在為 {message.author.display_name} 生成回覆: '{full_prompt}'...")

                # 顯示輸入狀態 (打字中)
                async with message.channel.typing():
                    reply_text = await ai_manager.generate_response(
                        guild_id=message.guild.id if message.guild else 0,
                        channel_id=message.channel.id,
                        user_id=message.author.id,
                        user_name=message.author.display_name,
                        user_prompt=full_prompt
                    )

                # 發送回覆 (引用發話者)
                await message.reply(reply_text, mention_author=False)
            except Exception as e:
                logger.error(f"[AI-Worker 異常]: {e}")
            finally:
                self.ai_queue.task_done()

    async def setup_hook(self):
        """機器人啟動時的初始化掛鉤"""
        # 啟動 AI 對話隊列背景工作緒
        self.ai_worker_task = asyncio.create_task(self.ai_worker())

        # 1. 初始化資料庫
        logger.info("📦 正在初始化 images.db 資料庫...")
        database.init_db()

        # 2. 初始化 Google Drive 連線
        logger.info("☁️ 正在初始化 Google Drive 雲端硬碟服務...")
        try:
            drive_client.init_service()
            logger.info("✅ Google Drive 服務連線成功！")
        except Exception as e:
            logger.error(f"❌ Google Drive 服務連線失敗: {e}")

        # 3. 檢查本地資料庫是否為空 (若不慎被刪除，自動自雲端硬碟拉取並重建)
        if database.is_database_empty():
            logger.info("⚠️ 偵測到本地資料庫為空，正在自 Google 雲端硬碟自動拉取並重建資料庫...")
            try:
                drive_files = await drive_client.list_files_async()
                stats = database.sync_from_google_drive(drive_files)
                logger.info(
                    f"🎉 資料庫自動重建完成！共掃描雲端 {len(drive_files)} 個檔案，"
                    f"成功匯入 {stats['added']} 筆圖片 (目前總數: {stats['total']} 筆)。"
                )
            except Exception as e:
                logger.error(f"❌ 自動重建資料庫失敗: {e}")

        # 4. 指令同步策略：優先同步至 GUILD_ID 伺服器 (秒級即時生效，徹底擺脫全域指令 1 小時快取延遲)
        if DEV_GUILD_ID and DEV_GUILD_ID.strip():
            try:
                guild_obj = discord.Object(id=int(DEV_GUILD_ID.strip()))
                self.tree.copy_global_to(guild=guild_obj)
                guild_synced = await self.tree.sync(guild=guild_obj)
                logger.info(f"⚡ 伺服器 ({DEV_GUILD_ID}) 指令同步成功！共 {len(guild_synced)} 個指令 (即時生效，含右鍵選單)")
            except Exception as e:
                logger.error(f"❌ 伺服器指令同步失敗: {e}")
        else:
            logger.info("🌐 正在向 Discord 註冊全域斜線指令...")
            try:
                global_synced = await self.tree.sync()
                logger.info(f"🌐 全域斜線指令同步完成！共同步 {len(global_synced)} 個指令")
            except Exception as e:
                logger.error(f"❌ 全域斜線指令同步失敗: {e}")


bot = MemeBot()


@bot.event
async def on_ready():
    divider = "=" * 50
    logger.info(divider)
    logger.info(f"🤖 機器人登入成功：{bot.user} (ID: {bot.user.id})")
    logger.info(f"服務伺服器數量: {len(bot.guilds)} 個")
    logger.info("指令模式: 圖片保存與檢索庫 (Google Drive 串接)")
    logger.info(divider)

    activity = discord.Activity(
        type=discord.ActivityType.watching,
        name="右鍵圖片存圖 或 @機器人 聊天"
    )
    await bot.change_presence(status=discord.Status.online, activity=activity)


# ================= 訊息監聽：儲存圖片 vs AI 對話 =================
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # 檢查是否標註機器人
    if bot.user in message.mentions:
        # 判斷是否為「儲存圖片」意圖：必須是「回覆某則訊息」且文字內包含雙引號檔案名稱「""、“”或「」」
        pattern = r'["“”「](.+?)["“”」]'
        is_image_save = (
            message.reference is not None and
            re.search(pattern, message.content) is not None
        )

        if is_image_save:
            # ================= [路徑 A] 原有 Google Drive 圖片儲存功能 =================
            logger.info(f"[除錯-存圖] 偵測到存圖請求 | 發送者: {message.author} (頻道: #{message.channel}) | 內容: '{message.content}'")

            match = re.search(pattern, message.content)
            base_name = match.group(1).strip() if match else ""
            if not base_name:
                logger.info("[除錯-存圖] 檔案名稱為空字串。")
                await message.reply("⚠️ 檔案名稱不能是空的！")
                return

            # 抓取被回覆的原始訊息
            try:
                replied_msg = await message.channel.fetch_message(message.reference.message_id)
            except discord.NotFound:
                logger.warning(f"[除錯-存圖] 找不到被回覆的原始訊息 (ID: {message.reference.message_id})")
                await message.reply("❌ 找不到原始訊息。")
                return
            except Exception as e:
                logger.error(f"[除錯-存圖] 讀取被回覆訊息失敗: {e}")
                await message.reply(f"❌ 讀取訊息失敗：{e}")
                return

            # 檢查圖片附件
            images = [att for att in replied_msg.attachments if att.content_type and att.content_type.startswith('image/')]

            if len(images) == 0:
                if replied_msg.embeds and replied_msg.embeds[0].image:
                    logger.info("[除錯-存圖] 原始訊息僅包含外連 Embed 圖片。")
                    await message.reply("⚠️ 偵測到外連 Embed 圖片，建議直接上傳圖片檔案以獲得最佳畫質儲存！")
                    return
                logger.info("[除錯-存圖] 原始訊息中未偵測到任何圖片。")
                await message.reply("⚠️ 這則訊息裡面沒有偵測到圖片檔案。")
                return
            elif len(images) > 1:
                logger.info(f"[除錯-存圖] 原始訊息包含多張圖片 ({len(images)} 張)。")
                await message.reply("⚠️ 這則訊息包含多張圖片，請一次只回覆單張圖片。")
                return

            image_att = images[0]

            # 處理名稱重複問題：同名時自動遞增 "名稱(2)", "名稱(3)"
            final_name = database.get_unique_name(base_name)
            logger.info(f"[除錯-存圖] 名稱確認: 原始='{base_name}' -> 最終存檔名='{final_name}'")

            try:
                await message.add_reaction("⏳")
            except discord.HTTPException:
                pass

            try:
                file_bytes = await image_att.read()
                mimetype = image_att.content_type or "image/png"
                
                orig_ext = os.path.splitext(image_att.filename)[1].lower()
                if not orig_ext:
                    orig_ext = mimetypes.guess_extension(mimetype) or ".png"

                full_filename = f"{final_name}{orig_ext}"
                logger.info(f"[除錯-GDrive] 開始串流上傳至雲端: '{full_filename}' ({mimetype}, 大小: {len(file_bytes)} bytes)...")

                drive_id = await drive_client.upload_async(
                    file_bytes=file_bytes,
                    filename=full_filename,
                    mimetype=mimetype
                )
                logger.info(f"[除錯-GDrive] 雲端上傳成功！取得 Drive ID: {drive_id}")

                database.add_image(
                    name=final_name,
                    drive_id=drive_id,
                    mimetype=mimetype,
                    extension=orig_ext,
                    uploader_name=message.author.display_name,
                    uploader_id=message.author.id
                )
                logger.info(f"[除錯-資料庫] 成功將圖片 '{final_name}' 記錄寫入 SQLite images.db")

                await message.reply(f"圖片`{final_name}`已成功保存")
            except Exception as e:
                logger.error(f"[除錯-存圖錯誤]: {e}")
                await message.reply(f"❌ 儲存失敗：{e}")
            finally:
                try:
                    await message.remove_reaction("⏳", bot.user)
                except Exception:
                    pass

        else:
            # ================= [路徑 B] 執行 AI 智能對話功能 =================
            if not message.guild:
                return

            # 伺服器與頻道範圍檢查 (嚴格遵守 .env 設定之 GUILD_ID 與 AI_CHANNEL_ID)
            if not ai_manager.is_allowed_scope(message.guild.id, message.channel.id):
                logger.info(f"[除錯-AI] 忽略對話：伺服器 ({message.guild.id}) 或頻道 ({message.channel.id}) 不在允許範圍。")
                return

            # 排入非同步隊列 (單線程依序回答，徹底避免短時間多則訊息競爭與卡死)
            q_len = bot.ai_queue.qsize() + 1
            logger.info(f"[除錯-AI] 收到 {message.author.display_name} 的對話請求，排入佇列 (第 {q_len} 順位)...")
            await bot.ai_queue.put(message)


# ================= 自動補全邏輯 (Autocomplete) =================
async def image_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    """即時模糊搜尋已有圖片名稱"""
    try:
        names = database.search_autocomplete(current, limit=25)
        preview_str = ", ".join(names[:5]) if names else "無符合選項"
        logger.info(f"[除錯-補全] 使用者 {interaction.user} 查詢: '{current}' | 匹配 {len(names)} 筆 ({preview_str})")
        return [
            app_commands.Choice(name=name[:100], value=name[:100])
            for name in names
        ]
    except Exception as e:
        logger.error(f"[除錯-補全異常]: {e}")
        return []


# ================= 2. 搜尋並發送功能：/search "圖片名稱" =================
@bot.tree.command(name="search", description="搜尋並發送儲存的圖片")
@app_commands.describe(圖片名稱="請輸入或選擇要搜尋的圖片名稱")
@app_commands.autocomplete(圖片名稱=image_autocomplete)
async def search_image(interaction: discord.Interaction, 圖片名稱: str):
    logger.info(f"[除錯-搜尋] 收到搜尋指令 | 請求者: {interaction.user} | 查詢圖片: '{圖片名稱}'")
    await interaction.response.defer()

    record = database.get_image(圖片名稱)
    if not record:
        logger.warning(f"[除錯-搜尋] 找不到圖片: '{圖片名稱}'")
        await interaction.followup.send(f"❌ 找不到名為 `{圖片名稱}` 的圖片。", ephemeral=True)
        return

    drive_id = record["drive_id"]
    extension = record.get("extension") or ".png"
    logger.info(f"[除錯-搜尋] 資料庫查得 Drive ID: {drive_id}, 格式: {extension}")

    try:
        # 從 Google Drive 記憶體串流下載
        logger.info(f"[除錯-GDrive] 正在從雲端串流下載圖片 (ID: {drive_id})...")
        fh = await drive_client.download_async(drive_id)
        size_bytes = fh.getbuffer().nbytes
        logger.info(f"[除錯-GDrive] 下載完成 (檔案大小: {size_bytes} bytes)，正在發送至 Discord...")

        discord_file = discord.File(fp=fh, filename=f"{圖片名稱}{extension}")
        await interaction.followup.send(content=f"🖼️ `{圖片名稱}`", file=discord_file)
        logger.info(f"[除錯-搜尋] 圖片 '{圖片名稱}' 發送完成！")
    except Exception as e:
        logger.error(f"[除錯-搜尋異常] 下載或發送圖片失敗: {e}")
        await interaction.followup.send(f"❌ 從雲端硬碟抓取圖片失敗：{e}")


# ================= 3. 刪除功能：/delete "圖片名稱" =================
@bot.tree.command(name="delete", description="刪除已儲存的圖片")
@app_commands.describe(圖片名稱="請選擇要刪除的圖片名稱")
@app_commands.autocomplete(圖片名稱=image_autocomplete)
async def delete_image(interaction: discord.Interaction, 圖片名稱: str):
    logger.info(f"[除錯-刪除] 收到刪除指令 | 請求者: {interaction.user} | 目標圖片: '{圖片名稱}'")
    await interaction.response.defer()

    record = database.get_image(圖片名稱)
    if not record:
        logger.warning(f"[除錯-刪除] 資料庫中找不到圖片: '{圖片名稱}'")
        await interaction.followup.send(f"❌ 找不到名為 `{圖片名稱}` 的圖片。", ephemeral=True)
        return

    drive_id = record["drive_id"]

    try:
        # 從 Google Drive 刪除
        logger.info(f"[除錯-GDrive] 正在從雲端刪除檔案 (ID: {drive_id})...")
        await drive_client.delete_async(drive_id)

        # 從 SQLite 刪除
        logger.info(f"[除錯-資料庫] 正在從本地 SQLite 移除記錄 '{圖片名稱}'...")
        database.delete_image(圖片名稱)

        logger.info(f"[除錯-刪除] 圖片 '{圖片名稱}' 已完全刪除成功！")
        await interaction.followup.send(f"🗑️ 圖片 `{圖片名稱}` 已成功刪除！")
    except Exception as e:
        logger.error(f"[除錯-刪除異常] 刪除失敗: {e}")
        await interaction.followup.send(f"❌ 刪除時發生錯誤：{e}")


# ================= 4. 直接上傳指令：/save "圖片名稱" "檔案" =================
@bot.tree.command(name="save", description="直接上傳並儲存圖片至雲端硬碟")
@app_commands.describe(
    圖片名稱="為這張圖片命名的名稱",
    圖片檔案="要上傳的圖片檔案"
)
async def save_image_cmd(
    interaction: discord.Interaction,
    圖片名稱: str,
    圖片檔案: discord.Attachment
):
    """供斜線指令直接上傳的快捷方式"""
    if not 圖片檔案.content_type or not 圖片檔案.content_type.startswith("image/"):
        logger.warning(f"[除錯-上傳] 檔案格式非圖片: {圖片檔案.content_type}")
        await interaction.response.send_message("❌ 請上傳有效的圖片檔案！", ephemeral=True)
        return

    await interaction.response.defer()

    clean_name = 圖片名稱.strip()
    final_name = database.get_unique_name(clean_name)
    logger.info(f"[除錯-上傳] 收到上傳請求 | 請求者: {interaction.user} | 目標檔名: '{final_name}' | 原始檔名: {圖片檔案.filename} ({圖片檔案.size} bytes)")

    try:
        file_bytes = await 圖片檔案.read()
        mimetype = 圖片檔案.content_type or "image/png"
        orig_ext = os.path.splitext(圖片檔案.filename)[1].lower()
        if not orig_ext:
            orig_ext = mimetypes.guess_extension(mimetype) or ".png"

        full_filename = f"{final_name}{orig_ext}"
        logger.info(f"[除錯-GDrive] 開始直接上傳 '{full_filename}' 到雲端硬碟...")
        drive_id = await drive_client.upload_async(
            file_bytes=file_bytes,
            filename=full_filename,
            mimetype=mimetype
        )
        logger.info(f"[除錯-GDrive] 上傳成功，Drive ID: {drive_id}")

        database.add_image(
            name=final_name,
            drive_id=drive_id,
            mimetype=mimetype,
            extension=orig_ext,
            uploader_name=interaction.user.display_name,
            uploader_id=interaction.user.id
        )
        logger.info(f"[除錯-資料庫] 已將 '{final_name}' 存入本地 images.db")

        await interaction.followup.send(f"圖片`{final_name}`已成功保存")
    except Exception as e:
        logger.error(f"[除錯-上傳異常] 上傳失敗: {e}")
        await interaction.followup.send(f"❌ 儲存失敗：{e}")


# ================= 4.5. 右鍵訊息快捷存圖選單 (Message Context Menu) =================
class SaveImageModal(discord.ui.Modal, title="儲存為梗圖"):
    image_name = discord.ui.TextInput(
        label="圖片名稱",
        placeholder="請輸入要儲存的圖片名稱（例如：哭死貓咪）",
        min_length=1,
        max_length=50,
        required=True
    )

    def __init__(self, target_message: discord.Message):
        super().__init__()
        self.target_message = target_message

    async def on_submit(self, interaction: discord.Interaction):
        clean_name = self.image_name.value.strip()
        if not clean_name:
            await interaction.response.send_message("⚠️ 圖片名稱不能是空的！", ephemeral=True)
            return

        # 延遲回應以避免上傳雲端時間過長導致超時
        await interaction.response.defer()

        # 1. 優先抓取附件圖片
        images = [
            att for att in self.target_message.attachments
            if (att.content_type and att.content_type.startswith("image/"))
            or att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        ]

        file_bytes = None
        mimetype = "image/png"
        orig_ext = ".png"

        try:
            if images:
                image_att = images[0]
                file_bytes = await image_att.read()
                mimetype = image_att.content_type or "image/png"
                orig_ext = os.path.splitext(image_att.filename)[1].lower()
                if not orig_ext:
                    orig_ext = mimetypes.guess_extension(mimetype) or ".png"
            elif self.target_message.embeds:
                # 2. 次要抓取外連 Embed 圖片或縮圖
                for embed in self.target_message.embeds:
                    img_url = (embed.image.url if embed.image else None) or (embed.thumbnail.url if embed.thumbnail else None)
                    if img_url:
                        async with aiohttp.ClientSession() as session:
                            async with session.get(img_url) as resp:
                                if resp.status == 200:
                                    file_bytes = await resp.read()
                                    mimetype = resp.headers.get("Content-Type", "image/png").split(";")[0]
                                    orig_ext = mimetypes.guess_extension(mimetype) or ".png"
                                    break

            if not file_bytes:
                await interaction.followup.send("❌ 這則訊息中找不到可讀取的圖片檔案！", ephemeral=True)
                return

            # 自動遞增同名圖片
            final_name = database.get_unique_name(clean_name)
            full_filename = f"{final_name}{orig_ext}"

            logger.info(f"[除錯-右鍵存圖] 使用者 {interaction.user} 透過選單儲存: '{full_filename}' ({mimetype}, 大小: {len(file_bytes)} bytes)")

            drive_id = await drive_client.upload_async(
                file_bytes=file_bytes,
                filename=full_filename,
                mimetype=mimetype
            )
            logger.info(f"[除錯-右鍵存圖] GDrive 上傳成功，Drive ID: {drive_id}")

            database.add_image(
                name=final_name,
                drive_id=drive_id,
                mimetype=mimetype,
                extension=orig_ext,
                uploader_name=interaction.user.display_name,
                uploader_id=interaction.user.id
            )
            logger.info(f"[除錯-右鍵存圖] 已記錄至本地資料庫: '{final_name}'")

            note = ""
            if len(images) > 1:
                note = f" (偵測到訊息有 {len(images)} 張圖片，已儲存第 1 張)"

            await interaction.followup.send(f"圖片`{final_name}`已成功保存")
        except Exception as e:
            logger.error(f"[除錯-右鍵存圖異常]: {e}")
            await interaction.followup.send(f"❌ 儲存失敗：{e}")


@bot.tree.context_menu(name="儲存為梗圖")
async def save_image_context_menu(interaction: discord.Interaction, message: discord.Message):
    """
    訊息右鍵選單：
    在任何圖片訊息點擊右鍵 (或手機長按) -> 應用程式 -> 儲存為梗圖
    """
    # 檢查訊息是否包含圖片附件或 Embed
    has_image = any(
        (att.content_type and att.content_type.startswith("image/"))
        or att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        for att in message.attachments
    )
    if not has_image and message.embeds:
        has_image = any(embed.image or embed.thumbnail for embed in message.embeds)

    if not has_image:
        await interaction.response.send_message("❌ 這則訊息中沒有偵測到任何圖片！", ephemeral=True)
        return

    # 彈出視窗讓使用者輸入名稱
    modal = SaveImageModal(target_message=message)
    await interaction.response.send_modal(modal)


# ================= 5. 資料庫同步/災難復原功能：/sync =================
@bot.tree.command(name="sync", description="從 Google 雲端硬碟同步並重建本地圖片資料庫 (災難復原)")
async def sync_database(interaction: discord.Interaction):
    """
    從 Google 雲端硬碟拉取所有檔案清單並同步至 SQLite 資料庫。
    特別適用於本地 images.db 不慎被刪除或資料不一致時！
    """
    logger.info(f"[除錯-同步] 使用者 {interaction.user} 觸發資料庫同步請求...")
    await interaction.response.defer()

    try:
        drive_files = await drive_client.list_files_async()
        logger.info(f"[除錯-同步] 從 Google 雲端硬碟抓取到 {len(drive_files)} 個檔案，正在比對同步...")
        stats = database.sync_from_google_drive(drive_files)
        logger.info(f"[除錯-同步] 同步完成: 新增={stats['added']}, 原有={stats['existing']}, 刪除={stats['deleted']}, 總數={stats['total']}")

        embed = discord.Embed(
            title="🔄 雲端硬碟資料庫同步完成",
            description="已成功比對 Google 雲端硬碟並更新本地 SQLite 資料庫！",
            color=discord.Color.green()
        )
        embed.add_field(name="☁️ 雲端硬碟檔案數", value=f"`{len(drive_files)}` 個", inline=True)
        embed.add_field(name="📥 本次新增補全", value=f"`{stats['added']}` 筆", inline=True)
        embed.add_field(name="🔄 原有保留", value=f"`{stats['existing']}` 筆", inline=True)
        if stats["deleted"] > 0:
            embed.add_field(name="🗑️ 清理雲端已刪除", value=f"`{stats['deleted']}` 筆", inline=True)
        embed.add_field(name="📊 目前資料庫總筆數", value=f"`{stats['total']}` 筆", inline=True)
        embed.set_footer(text=f"執行者：{interaction.user.display_name}")

        await interaction.followup.send(embed=embed)
    except Exception as e:
        logger.error(f"[除錯-同步異常] 手動同步失敗: {e}")
        await interaction.followup.send(f"❌ 同步 Google 雲端硬碟資料庫失敗：{e}")


# ================= 6. 圖片清單功能：/list =================
@bot.tree.command(name="list", description="查看已儲存的圖片清單與統計數量 (僅自己可見)")
async def list_cmd(interaction: discord.Interaction):
    # 設定為 ephemeral=True，僅觸發指令的用戶看得到訊息，避免洗版
    await interaction.response.defer(ephemeral=True)

    items, total = database.list_images(limit=25)
    logger.info(f"[除錯-清單] 使用者 {interaction.user} 檢視圖片庫清單 | 目前雲端圖庫總數: {total} 張")

    if total == 0:
        await interaction.followup.send("ℹ️ 目前尚未儲存任何圖片。", ephemeral=True)
        return

    embed = discord.Embed(
        title="📚 圖片庫清單",
        description=f"目前雲端硬碟共儲存 **{total}** 張圖片：",
        color=discord.Color.blue()
    )

    names_str = "、".join([f"`{item['name']}`" for item in items])
    embed.add_field(name="最近儲存的圖片 (前 25 筆)", value=names_str, inline=False)
    embed.set_footer(text="提示：使用 /search <圖片名稱> 即可隨時叫出圖片")

    await interaction.followup.send(embed=embed, ephemeral=True)


# ================= 7. 清理伺服器舊指令快取：/clearcache =================
@bot.tree.command(name="clearcache", description="清除本伺服器舊指令殘留並重新覆蓋同步 (管理員限定)")
@app_commands.default_permissions(administrator=True)
async def clear_cache_cmd(interaction: discord.Interaction):
    """
    一鍵清除特定伺服器內殘留的舊指令 (如已刪除的 /random)，並覆蓋成最新的指令集。
    """
    await interaction.response.defer(ephemeral=True)
    if interaction.guild:
        logger.info(f"[除錯-快取] 使用者 {interaction.user} 於伺服器 '{interaction.guild.name}' ({interaction.guild.id}) 執行伺服器專屬指令清除...")
        # 同步最新的伺服器專屬指令集
        bot.tree.copy_global_to(guild=interaction.guild)
        synced = await bot.tree.sync(guild=interaction.guild)
        logger.info(f"[除錯-快取] 伺服器指令已同步，目前生效指令共 {len(synced)} 個")

        await interaction.followup.send(
            f"✅ **已成功同步本伺服器所有最新指令！**\n"
            f"目前生效的指令共 `{len(synced)}` 個（含斜線指令與右鍵『儲存為梗圖』選單）。\n\n"
            f"💡 **重要步驟**：若 Discord 用戶端未即時顯示，請在視窗內按下 **`Ctrl + R`** 重新整理！",
            ephemeral=True
        )
    else:
        await interaction.followup.send("❌ 此指令僅能在伺服器內執行。", ephemeral=True)


# ================= 8. 清除頻道 AI 對話歷史：/clearchat =================
@bot.tree.command(name="clearchat", description="清除本頻道的本地 AI 對話歷史記憶 (管理員限定)")
@app_commands.default_permissions(administrator=True)
async def clear_chat_cmd(interaction: discord.Interaction):
    """
    清除當前頻道的本地 50 筆對話紀錄，使 AI 對話記憶重置。
    """
    await interaction.response.defer(ephemeral=True)
    if not interaction.guild:
        await interaction.followup.send("❌ 此指令僅能在伺服器內執行。", ephemeral=True)
        return

    count = database.clear_chat_history(str(interaction.channel.id))
    logger.info(f"[除錯-AI] 使用者 {interaction.user} 清除了頻道 #{interaction.channel} 的對話紀錄 (共 {count} 筆)")
    await interaction.followup.send(
        f"🧹 已清除本頻道儲存的 AI 對話歷史（共清除 `{count}` 則訊息），對話記憶已重置！",
        ephemeral=True
    )

# ================= 全域錯誤捕獲 =================
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # 若為舊版快取中已刪除或未註冊的指令，記錄警告並略過
    if isinstance(error, app_commands.CommandNotFound):
        logger.warning(f"收到未註冊或已過期的指令請求: {error}")
        return

    logger.error(f"AppCommand 全域錯誤: {error}")
    try:
        if interaction.response.is_done():
            await interaction.followup.send("❌ 執行指令時發生錯誤。", ephemeral=True)
        else:
            await interaction.response.send_message("❌ 執行指令時發生未預期的錯誤。", ephemeral=True)
    except Exception:
        pass


# ================= 啟動主入口 =================
def main():
    if not TOKEN or not TOKEN.strip():
        logger.error("❌ 未設定 DISCORD_TOKEN！請至 .env 檔案填寫。")
        return

    if not FOLDER_ID or not FOLDER_ID.strip():
        logger.error("❌ 未設定 GDRIVE_FOLDER_ID！請至 .env 檔案填寫目標雲端硬碟資料夾 ID。")
        return

    bot.run(TOKEN)


if __name__ == '__main__':
    main()
