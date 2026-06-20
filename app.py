
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import gspread
from google.oauth2.service_account import Credentials
import os
import json
import base64
import httpx
from datetime import datetime
import traceback

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

class BulkEntries(BaseModel):
    entries: List[Entry]


@app.get("/")
def index():
    return FileResponse("static/index.html")


@app.post("/add")
def add_entry(entry: Entry):
    sheet = get_sheet()
    col_c_values = sheet.col_values(3)
    target_row = 2
    for i, val in enumerate(col_c_values[1:], start=2):
        if not val or val.strip() == "":
            target_row = i
            break
    else:
        target_row = len(col_c_values) + 1
    range_name = f"C{target_row}:G{target_row}"
    row_data = [entry.date, entry.category, entry.memo, entry.expense_type, entry.amount]
    sheet.update(range_name, [row_data], value_input_option="USER_ENTERED")
    return {"status": "ok", "message": "記録しました"}


@app.post("/add_bulk")
def add_bulk(body: BulkEntries):
    """複数行を一括でスプレッドシートに書き込む"""
    sheet = get_sheet()
    col_c_values = sheet.col_values(3)

    # 最初の空き行を探す
    target_row = 2
    for i, val in enumerate(col_c_values[1:], start=2):
        if not val or val.strip() == "":
            target_row = i
            break
    else:
        target_row = len(col_c_values) + 1

    rows_data = []
    for entry in body.entries:
        rows_data.append([entry.date, entry.category, entry.memo, entry.expense_type, entry.amount])

    end_row = target_row + len(rows_data) - 1
    range_name = f"C{target_row}:G{end_row}"
    sheet.update(range_name, rows_data, value_input_option="USER_ENTERED")
    return {"status": "ok", "message": f"{len(rows_data)}件記録しました"}


@app.get("/history")
def get_history(limit: int = 10):
    sheet = get_sheet()
    all_values = sheet.get_all_values()
    data_rows = all_values[1:] if all_values else []
    rows = []
    for row in data_rows:
        if len(row) <= 2 or not row[2] or row[2].strip() == "":
            continue
        c_to_g = row[2:7]
        while len(c_to_g) < 5:
            c_to_g.append("")
        rows.append({
            "date": c_to_g[0],
            "category": c_to_g[1],
            "expense_type": c_to_g[3],
            "amount": c_to_g[4],
            "memo": c_to_g[2],
        })
    return {"rows": rows[-limit:][::-1]}


@app.post("/scan")
async def scan_receipt(file: UploadFile = File(...)):
    gemini_api_key = os.environ.get("GEMINI_API_KEY")
    if not gemini_api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY が設定されていません")

    try:
        image_data = await file.read()
        image_b64 = base64.b64encode(image_data).decode("utf-8")
        mime_type = file.content_type or "image/jpeg"
        today = datetime.now().strftime("%Y-%m-%d")

        prompt = f"""このレシート画像から家計簿の情報を抽出してください。
今日の日付は {today} です。

同じカテゴリの品目は合算して1行にまとめてください。
カテゴリが複数ある場合は複数の要素を返してください。

以下のJSON配列形式のみで返してください。説明文・コードブロックは不要です。

[
  {{
    "date": "YYYY-MM-DD形式（レシートに日付があればそれ、なければ今日）",
    "category": "以下から最も適切なものを1つ: 食費, 外食, 交通費, 光熱費, 通信費, 医療費, 日用品, 衣服, 娯楽, 教育, 保険, その他",
    "expense_type": "以下から1つ: 固定費, 変動費, 特別支出, 貯蓄",
    "amount": 合計金額を整数で,
    "memo": "カテゴリの内容を簡潔に（例: 野菜・肉類）"
  }}
]"""

        payload = {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime_type, "data": image_b64}}
                ]
            }]
        }

        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={gemini_api_key}"
        async with httpx.AsyncClient(timeout=30) as client:
            res = await client.post(url, json=payload)

        if res.status_code != 200:
            raise HTTPException(status_code=500, detail=f"Gemini APIエラー: {res.text}")

        result = res.json()
        text = result["candidates"][0]["content"]["parts"][0]["text"]
        text = text.strip().strip("```json").strip("```").strip()
        parsed = json.loads(text)

        # 単一オブジェクトが返ってきた場合も配列に統一
        if isinstance(parsed, dict):
            parsed = [parsed]

        return {"entries": parsed}

    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"サーバー内部エラー: {str(e)}")


app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)
