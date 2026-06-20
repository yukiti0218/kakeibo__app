from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import gspread
from google.oauth2.service_account import Credentials
import os
import json
import base64
import httpx
from datetime import datetime

app = FastAPI()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_sheet():
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise HTTPException(status_code=500, detail="GOOGLE_CREDENTIALS_JSON が設定されていません")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise HTTPException(status_code=500, detail="SPREADSHEET_ID が設定されていません")
    sheet_name = os.environ.get("SHEET_NAME", "Sheet1")
    return client.open_by_key(spreadsheet_id).worksheet(sheet_name)


class Entry(BaseModel):
    date: str
    category: str
    expense_type: str
    amount: int
    memo: Optional[str] = ""


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.post("/add")
def add_entry(entry: Entry):
    sheet = get_sheet()
    row_data = [entry.date, entry.category, entry.expense_type, entry.amount, entry.memo]
    sheet.append_row(row_data, value_input_option="USER_ENTERED")
    return {"status": "ok", "message": "記録しました"}


@app.get("/history")
def get_history(limit: int = 10):
    sheet = get_sheet()
    all_values = sheet.get_all_values()
    rows = all_values[-limit:] if len(all_values) > 0 else []
    history = []
    for row in reversed(rows):
        if len(row) >= 4:
            history.append({
                "date": row[0],
                "category": row[1],
                "expense_type": row[2],
                "amount": row[3],
                "memo": row[4] if len(row) > 4 else "",
            })
    return {"history": history}


@app.post("/scan")
async def scan_receipt(file: UploadFile = File(...)):
    """レシート画像をGeminiで解析して項目を返す"""
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY が設定されていません")

    image_data = await file.read()
    image_b64 = base64.b64encode(image_data).decode("utf-8")
    mime_type = file.content_type or "image/jpeg"

    today = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""このレシート画像から家計簿の情報を抽出してください。
今日の日付は {today} です。

以下のJSON形式のみで返してください。説明文は不要です。

{{
  "date": "YYYY-MM-DD形式の日付（レシートに日付があればそれを使い、なければ今日の日付）",
  "category": "以下から最も適切なものを1つ選ぶ: 食費, 外食, 交通費, 光熱費, 通信費, 医療費, 日用品, 衣服, 娯楽, 教育, 保険, その他",
  "expense_type": "以下から最も適切なものを1つ選ぶ: 固定費, 変動費, 特別支出, 貯蓄",
  "amount": 合計金額を整数で（円記号なし）,
  "memo": "店名や購入内容を簡潔に"
}}"""

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": image_b64
                        }
                    }
                ]
            }
        ]
    }

    # 正しいGemini APIのエンドポイントURL
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={gemini_api_key}"

    async with httpx.AsyncClient(timeout=30) as client:
        res = await client.post(url, json=payload)

    if res.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Gemini APIエラー: {res.text}")

    result = res.json()
    try:
        text = result["candidates"][0]["content"]["parts"][0]["text"]
        # JSON部分だけ抽出
        text = text.strip().strip("```json").strip("```").strip()
        parsed = json.loads(text)
        return parsed
    except (KeyError, IndexError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=500, detail=f"Geminiからのレスポンス解析に失敗しました: {str(e)}")