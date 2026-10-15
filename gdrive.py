import os
import io
import asyncio
import logging
from typing import Optional
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2 import service_account
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("GoogleDrive")

SCOPES = ['https://www.googleapis.com/auth/drive']


class GoogleDriveService:
    def __init__(self):
        self.service = None
        self.folder_id = (
            os.getenv('GDRIVE_FOLDER_ID') or os.getenv('GOOGLE_DRIVE_FOLDER_ID') or ""
        ).strip()
        self.service_account_file = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', 'service_account.json').strip()

    def get_folder_id(self) -> str:
        if not self.folder_id:
            self.folder_id = (
                os.getenv('GDRIVE_FOLDER_ID') or os.getenv('GOOGLE_DRIVE_FOLDER_ID') or ""
            ).strip()
        return self.folder_id

    def init_service(self):
        """
        智慧雙認證模式：
        1. 優先檢查是否具有 Google 服務帳號金鑰 (service_account.json)，適合機器人 24 小時無人值守運作。
        2. 若無服務帳號金鑰，則切換至 OAuth 2.0 使用者憑證 (token.json / credentials.json)。
        """
        if self.service is not None:
            return self.service

        # --- 模式 1: Google 服務帳號 (Service Account) ---
        sa_path = self.service_account_file
        if not os.path.isabs(sa_path):
            sa_path = os.path.join(os.path.dirname(__file__), sa_path)

        # 僅在非 token.json 且非 credentials.json 時嘗試以服務帳號解析
        is_user_oauth_file = sa_path.endswith("token.json") or sa_path.endswith("credentials.json")
        if os.path.exists(sa_path) and not is_user_oauth_file:
            try:
                creds = service_account.Credentials.from_service_account_file(
                    sa_path, scopes=SCOPES
                )
                self.service = build('drive', 'v3', credentials=creds, cache_discovery=False)
                logger.info("🔑 已透過 Google 服務帳號 (Service Account) 成功登入 Google Drive！")
                return self.service
            except Exception as e:
                logger.warning(f"服務帳號驗證未成功，嘗試切換至 OAuth 2.0: {e}")

        # --- 模式 2: OAuth 2.0 個人使用者憑證 ---
        token_path = os.path.join(os.path.dirname(__file__), 'token.json')
        creds_path = os.path.join(os.path.dirname(__file__), 'credentials.json')

        creds = None
        if os.path.exists(token_path):
            try:
                creds = Credentials.from_authorized_user_file(token_path, SCOPES)
            except Exception as e:
                logger.warning(f"讀取 token.json 失敗: {e}")

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not os.path.exists(creds_path):
                    raise FileNotFoundError(
                        "找不到 Google Drive 驗證憑證！請確認專案中存在：\n"
                        "1. `service_account.json` (Google 服務帳號金鑰)\n"
                        "或 2. `credentials.json` (Google OAuth 桌面應用程式憑證)"
                    )
                flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
                creds = flow.run_local_server(port=0)

            with open(token_path, 'w', encoding='utf-8') as token:
                token.write(creds.to_json())

        self.service = build('drive', 'v3', credentials=creds, cache_discovery=False)
        logger.info("🔑 已透過 Google OAuth 2.0 成功登入 Google Drive！")
        return self.service

    # ================= 核心同步操作 =================

    def upload_to_drive_sync(self, file_bytes: bytes, filename: str, mimetype: str) -> str:
        """記憶體直接上傳至 Google Drive (不需佔用硬碟檔案)"""
        svc = self.init_service()
        folder = self.get_folder_id()
        if not folder:
            raise ValueError("請先於 .env 設定 GDRIVE_FOLDER_ID！")

        file_metadata = {
            'name': filename,
            'parents': [folder]
        }
        media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mimetype, resumable=True)
        file = svc.files().create(
            body=file_metadata,
            media_body=media,
            fields='id',
            supportsAllDrives=True
        ).execute()
        return file.get('id')

    def download_from_drive_sync(self, drive_id: str) -> io.BytesIO:
        """從 Google Drive 下載檔案流至記憶體中"""
        svc = self.init_service()
        request = svc.files().get_media(fileId=drive_id, supportsAllDrives=True)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        return fh

    def delete_from_drive_sync(self, drive_id: str) -> bool:
        """從 Google Drive 刪除檔案"""
        svc = self.init_service()
        try:
            svc.files().delete(fileId=drive_id, supportsAllDrives=True).execute()
            return True
        except HttpError as e:
            if e.resp.status == 404:
                return True
            logger.error(f"[GDrive 警告] 刪除檔案失敗 (ID: {drive_id}): {e}")
            raise e

    def list_files_in_folder_sync(self) -> list:
        """列出目標雲端資料夾中的所有檔案 (支援分頁遍歷，適用海量圖片)"""
        svc = self.init_service()
        folder = self.get_folder_id()
        if not folder:
            raise ValueError("請先於 .env 設定 GDRIVE_FOLDER_ID！")

        query = f"'{folder}' in parents and trashed = false"
        files = []
        page_token = None

        while True:
            response = svc.files().list(
                q=query,
                spaces='drive',
                fields='nextPageToken, files(id, name, mimeType, createdTime)',
                pageToken=page_token,
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()

            files.extend(response.get('files', []))
            page_token = response.get('nextPageToken')
            if not page_token:
                break

        return files

    # ================= 非同步背景執行緒封裝 =================

    async def upload_async(self, file_bytes: bytes, filename: str, mimetype: str) -> str:
        return await asyncio.to_thread(self.upload_to_drive_sync, file_bytes, filename, mimetype)

    async def download_async(self, drive_id: str) -> io.BytesIO:
        return await asyncio.to_thread(self.download_from_drive_sync, drive_id)

    async def delete_async(self, drive_id: str) -> bool:
        return await asyncio.to_thread(self.delete_from_drive_sync, drive_id)

    async def list_files_async(self) -> list:
        return await asyncio.to_thread(self.list_files_in_folder_sync)


# 全域實例
drive_client = GoogleDriveService()
