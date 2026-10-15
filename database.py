import sqlite3
import os
import mimetypes
from typing import Optional, List, Dict, Any, Tuple

DB_PATH = os.path.join(os.path.dirname(__file__), "images.db")


def get_connection() -> sqlite3.Connection:
    """取得具備 WAL 模式與線程安全設定的資料庫連線"""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def init_db() -> None:
    """初始化 SQLite images 資料庫，並自動相容升級欄位"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS images (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                drive_id TEXT NOT NULL,
                mimetype TEXT DEFAULT 'image/png',
                extension TEXT DEFAULT '.png',
                uploader_name TEXT,
                uploader_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 欄位自動相容檢測 (相容舊版 images.db 僅有 id, name, drive_id)
        cursor.execute("PRAGMA table_info(images)")
        existing_cols = [row["name"] for row in cursor.fetchall()]
        if "mimetype" not in existing_cols:
            cursor.execute("ALTER TABLE images ADD COLUMN mimetype TEXT DEFAULT 'image/png'")
        if "extension" not in existing_cols:
            cursor.execute("ALTER TABLE images ADD COLUMN extension TEXT DEFAULT '.png'")
        if "uploader_name" not in existing_cols:
            cursor.execute("ALTER TABLE images ADD COLUMN uploader_name TEXT")
        if "uploader_id" not in existing_cols:
            cursor.execute("ALTER TABLE images ADD COLUMN uploader_id INTEGER")
        if "created_at" not in existing_cols:
            cursor.execute("ALTER TABLE images ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

        # 為 name 欄位建立加速索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_images_name ON images(name);")

        # AI 對話歷史表 (支援上下文記憶，本地保存 50 筆)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ai_chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id TEXT,
                channel_id TEXT,
                user_id TEXT,
                user_name TEXT,
                role TEXT,
                content TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_channel ON ai_chat_history(channel_id, id);")
        conn.commit()


def get_unique_name(base_name: str) -> str:
    """自動處理同名問題：若名稱已存在，自動加上 (2), (3)..."""
    clean_name = base_name.strip()
    with get_connection() as conn:
        cursor = conn.cursor()
        final_name = clean_name
        counter = 2
        while True:
            cursor.execute("SELECT id FROM images WHERE name = ?", (final_name,))
            if cursor.fetchone() is None:
                return final_name
            final_name = f"{clean_name}({counter})"
            counter += 1


def add_image(
    name: str,
    drive_id: str,
    mimetype: str = "image/png",
    extension: str = ".png",
    uploader_name: Optional[str] = None,
    uploader_id: Optional[int] = None
) -> int:
    """新增圖片記錄到資料庫"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO images (name, drive_id, mimetype, extension, uploader_name, uploader_id)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (name, drive_id, mimetype, extension, uploader_name, uploader_id))
        conn.commit()
        return cursor.lastrowid


def get_image(name: str) -> Optional[Dict[str, Any]]:
    """依名稱精確取得圖片記錄"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM images WHERE name = ?", (name.strip(),))
        row = cursor.fetchone()
        return dict(row) if row else None


def search_autocomplete(query: str, limit: int = 25) -> List[str]:
    """供斜線指令 Autocomplete 使用的模糊搜尋 (限制 25 筆)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        if query and query.strip():
            cursor.execute(
                "SELECT name FROM images WHERE name LIKE ? ORDER BY id DESC LIMIT ?",
                (f"%{query.strip()}%", limit)
            )
        else:
            cursor.execute("SELECT name FROM images ORDER BY id DESC LIMIT ?", (limit,))
        
        rows = cursor.fetchall()
        return [str(row["name"]) for row in rows]


def delete_image(name: str) -> Optional[Dict[str, Any]]:
    """從資料庫刪除圖片記錄並回傳原記錄"""
    record = get_image(name)
    if not record:
        return None
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM images WHERE name = ?", (name.strip(),))
        conn.commit()
    return record


def is_database_empty() -> bool:
    """檢查資料庫是否為空 (例如不慎被刪除或剛初始化)"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as count FROM images")
        return cursor.fetchone()["count"] == 0


def sync_from_google_drive(drive_files: List[Dict[str, Any]]) -> Dict[str, int]:
    """
    從 Google 雲端硬碟檔案清單同步重建本地 SQLite 資料庫：
    - 若本地資料庫遺失/被刪，自動遍歷雲端檔案並重建所有記錄。
    - 若已有部分記錄，僅補足新檔案，並移除雲端已不存在的孤立記錄。
    """
    IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
    added_count = 0
    existing_count = 0
    deleted_count = 0

    with get_connection() as conn:
        cursor = conn.cursor()

        # 1. 取得目前本地所有 drive_id
        cursor.execute("SELECT id, name, drive_id FROM images")
        db_records = cursor.fetchall()
        db_drive_map = {row["drive_id"]: row["name"] for row in db_records}

        drive_file_ids = set()

        for f in drive_files:
            drive_id = f.get("id")
            raw_name = f.get("name", "未命名圖片")
            mimetype = f.get("mimeType", "image/png")
            created_at = f.get("createdTime")

            if not drive_id:
                continue

            drive_file_ids.add(drive_id)

            if drive_id in db_drive_map:
                existing_count += 1
                continue

            # 解析檔案名稱與副檔名
            base_name, ext = os.path.splitext(raw_name)
            ext_lower = ext.lower()
            if ext_lower in IMAGE_EXTS:
                image_name = base_name.strip()
                image_ext = ext_lower
            else:
                image_name = raw_name.strip()
                image_ext = mimetypes.guess_extension(mimetype) or ".png"

            # 確保名稱在資料庫內唯一
            final_name = get_unique_name(image_name)

            cursor.execute("""
                INSERT INTO images (name, drive_id, mimetype, extension, created_at)
                VALUES (?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))
            """, (final_name, drive_id, mimetype, image_ext, created_at))
            added_count += 1

        # 2. 清理在 Google Drive 上已被手動刪除的舊記錄
        for row in db_records:
            if row["drive_id"] not in drive_file_ids:
                cursor.execute("DELETE FROM images WHERE id = ?", (row["id"],))
                deleted_count += 1

        conn.commit()

        # 取得最新總數
        cursor.execute("SELECT COUNT(*) as total FROM images")
        total_count = cursor.fetchone()["total"]

    return {
        "added": added_count,
        "existing": existing_count,
        "deleted": deleted_count,
        "total": total_count
    }


def list_images(limit: int = 25, offset: int = 0) -> Tuple[List[Dict[str, Any]], int]:
    """分頁列出圖片與總數"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) as total FROM images")
        total = cursor.fetchone()["total"]

        cursor.execute(
            "SELECT * FROM images ORDER BY id DESC LIMIT ? OFFSET ?",
            (limit, offset)
        )
        items = [dict(row) for row in cursor.fetchall()]
        return items, total


def add_chat_message(
    guild_id: str,
    channel_id: str,
    user_id: str,
    user_name: str,
    role: str,
    content: str
) -> None:
    """儲存一則對話記錄，並自動保持該頻道最多只保留最新 50 則訊息"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO ai_chat_history (guild_id, channel_id, user_id, user_name, role, content)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (str(guild_id), str(channel_id), str(user_id), user_name, role, content))

        # 自動清理：保留最新 50 筆，刪除超過 50 筆的舊資料
        cursor.execute("""
            DELETE FROM ai_chat_history
            WHERE channel_id = ? AND id NOT IN (
                SELECT id FROM ai_chat_history
                WHERE channel_id = ?
                ORDER BY id DESC
                LIMIT 50
            )
        """, (str(channel_id), str(channel_id)))
        conn.commit()


def get_chat_history(channel_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """取得該頻道最新 50 則對話紀錄，按時間正序排列供 AI 參考上下文"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, guild_id, channel_id, user_id, user_name, role, content, created_at
            FROM (
                SELECT id, guild_id, channel_id, user_id, user_name, role, content, created_at
                FROM ai_chat_history
                WHERE channel_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id ASC
        """, (str(channel_id), limit))
        return [dict(row) for row in cursor.fetchall()]


def clear_chat_history(channel_id: str) -> int:
    """清除特定頻道的對話歷史記錄"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM ai_chat_history WHERE channel_id = ?", (str(channel_id),))
        conn.commit()
        return cursor.rowcount
