# 🖼️ Discord Google 雲端硬碟 圖片梗圖庫 & AI 對話機器人 (升級版)

本專案集成了 **「圖片儲存檢索系統」** 與 **「Google Gemini AI 智慧日常聊天系統」**，在完全不影響原有用法的前提下，提供雙軌智能運作！

---

## ✨ 核心功能與使用方式

### 1. 圖片保存方式（兩種超直覺途徑）
* **途徑 A【右鍵/長按快捷選單】（⭐ 最推薦，100% 不誤觸 AI）**：
  * 在任何圖片訊息點**右鍵（手機版長按）➡️「應用程式」➡️ 點選「儲存為梗圖」**。
  * 彈出小視窗輸入名稱後按下送出，直接秒存入 Google 雲端硬碟！
* **途徑 B【回覆標註 @機器人】**：
  * 當你**回覆（Reply）**某張圖片訊息，並標註 `@機器人 "名稱"`（支援 `""`、`“”`、`「」`）。
  * 存圖請務必加上引號（例如：`@機器人 "名稱"`），未帶引號之標註則進入一般 AI 對話。
* **智慧同名遞增**：若檔名已存在，自動命名為 `名稱(2)`、`名稱(3)`，永不覆蓋。

---

### 2. 🤖 AI 智慧對話系統特性
* 當你純標註 `@機器人` 聊天、提問、或閒聊吐槽時，機器人自動轉入 AI 對話引擎。
* **自主聯網搜尋**：自動判斷是否呼叫搜尋工具查證最新網路迷因或動漫時事。
* **真人口吻日常對話**：主要使用繁體中文，乾脆直接（1~2 句），不刻意使用奇怪贅字與粗話。

---

### 3. 指令與選單一覽

| 指令 / 操作 | 說明 | 權限 / 可見度 |
| :--- | :--- | :--- |
| **右鍵圖片 ➡️ 應用程式 ➡️ 儲存為梗圖** | 快捷彈窗命名並儲存圖片（手機長按訊息亦可） | 全員可用 |
| `/search <圖片名稱>` | 搜尋並發送儲存的圖片（支援即時打字自動補全） | 全員可用、公開發送 |
| `/save <圖片名稱> <圖片檔案>` | 直接上傳新圖片至 Google 雲端硬碟與資料庫 | 全員可用 |
| `/delete <圖片名稱>` | 刪除指定的圖片（同步自雲端與本地資料庫移除） | 全員可用 |
| `/list` | 檢視已儲存的圖片清單與總數量 | **僅自己可見 (Ephemeral)** |
| `/sync` | 雙向同步 Google 雲端硬碟與本地資料庫，支援災難復原 | 全員可用 |
| `/clearchat` | 清除本頻道的本地 50 則 AI 對話記憶（重新開始新話題） | **管理員限定 (Ephemeral)** |
| `/clearcache` | 清除伺服器舊指令殘留並重新覆蓋同步（解決指令重複兩次問題） | **管理員限定 (Ephemeral)** |

---

## 🛠️ 環境變數設定 (`.env`)

在專案目錄建立或編輯 `.env` 檔案：

```env
# 1. Discord 機器人 Token
DISCORD_TOKEN=你的_DISCORD_BOT_TOKEN

# 2. Google 雲端硬碟目標資料夾 ID
GDRIVE_FOLDER_ID=你的_GOOGLE_DRIVE_FOLDER_ID

# 3. 指定運作的伺服器 ID (AI 聊天僅在此伺服器運作，並享有秒級指令同步)
GUILD_ID=你的_GUILD_ID

# 4. Google 憑證檔案 (支援 service_account.json 或 token.json)
GOOGLE_SERVICE_ACCOUNT_FILE=token.json

# 5. Google Gemini API 金鑰 (至 https://aistudio.google.com/app/apikey 免費申請)
GEMINI_API_KEY=你的_GEMINI_API_KEY

# 6. (選填) Gemini 模型名稱，預設為 gemini-2.0-flash-lite
GEMINI_MODEL=gemini-2.0-flash-lite

# 7. (選填) AI 對話限定頻道 ID (若留空則在整個 GUILD_ID 伺服器均可聊)
AI_CHANNEL_ID=
```

---

## 🚀 快速啟動

1. **安裝依賴套件**：
   ```powershell
   pip install -r requirements.txt
   ```
2. **啟動機器人**：
   ```powershell
   python main.py
   ```
