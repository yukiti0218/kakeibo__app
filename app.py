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
import re

app = FastAPI()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

def get_sheet(sheet_name_env="SHEET_NAME", default_name="Sheet1"):
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise HTTPException(status_code=500, detail="GOOGLE_CREDENTIALS_JSON が設定されていません")
    creds_dict = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    spreadsheet_id = os.environ.get("SPREADSHEET_ID")
    if not spreadsheet_id:
        raise HTTPException(status_code=500, detail="SPREADSHEET_ID が設定されていません")
    
    spreadsheet = client.open_by_key(spreadsheet_id)
    name = os.environ.get(sheet_name_env, default_name)
    
    try:
        return spreadsheet.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=name, rows="100", cols="5")
        if name == "学習マスタ":
            ws.update("A1:B1", [["店名・キーワード", "正しいカテゴリ"]])
        return ws


def save_learning_data(memo: str, category: str):
    """ユーザーが登録した店名とカテゴリを学習マスタに保存・更新する"""
    if not memo or not category:
        return
    
    # メモから店名を切り出し（例: "イオン / 野菜" -> "イオン"）
    shop_name = memo.split("/")[0].strip()
    if len(shop_name) < 2:
        return

    try:
        master_sheet = get_sheet("MASTER_SHEET_NAME", "学習マスタ")
        all_rules = master_sheet.get_all_values()
        
        exists = False
        for idx, row in enumerate(all_rules):
            if idx == 0: continue
            if len(row) >= 1 and row[0] == shop_name:
                if len(row) >= 2 and row[1] != category:
                    master_sheet.update_cell(idx + 1, 2, category)
                exists = True
                break
        
        if not exists:
            master_sheet.append_row([shop_name, category], value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"[WARNING] 学習データの保存に失敗しました: {e}")


def load_learning_rules() -> str:
    """学習マスタから過去のルールを読み込んでプロンプト用のテキストにする"""
    try:
        master_sheet = get_sheet("MASTER_SHEET_NAME", "学習マスタ")
        all_rules = master_sheet.get_all_values()
        if len(all_rules) <= 1:
            return "（過去の学習データはまだありません）"
        
        rules_str = ""
        for row in all_rules[1:]:
            if len(row) >= 2 and row[0] and row[1]:
                rules_str += f"- 店名や内容に「{row[0]}」が含まれる場合 ➔ カテゴリは「{row[1]}」に分類する\n"
        return rules_str
    except Exception as e:
        print(f"[WARNING] 学習データの読み込みに失敗しました: {e}")
        return "（過去の学習データの読み込みに失敗しました）"


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
    
    save_learning_data(entry.memo, entry.category)
    return {"status": "ok", "message": "記録しました"}


@app.post("/add_bulk")
def add_bulk(body: BulkEntries):
    """複数行を一括でスプレッドシートに書き込む"""
    sheet = get_sheet()
    col_c_values = sheet.col_values(3)

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
        save_learning_data(entry.memo, entry.category)

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

        # 過去の学習マスタを読み込み
        learning_rules = load_learning_rules()

        # プロンプトの強化（税込の徹底 ＆ 純粋な数値の指定）
        prompt = f"""このレシート画像から家計簿の情報を抽出してください。
今日の日付は {today} です。

【金額抽出の厳格なルール】
1. 各品目の金額、および合計金額は「消費税（8%または10%）」を含んだ【税込価格】を抽出・算出してください。税抜価格（小計など）は絶対に無視してください。
2. amount に指定する値は「純粋な整数（数値）」のみにしてください。円マーク(￥)、英字、カンマ(,)、スペースなどの文字列は絶対に含めないでください。（例: ⭕ 1280, ❌ "￥1,280"）

同じカテゴリ of 品目は、それぞれの【税込金額】を合算して1行にまとめてください。
カテゴリが複数ある場合は複数の要素を返してください。

【最優先の分類ルール】
過去にユーザーが設定した以下のルールがある場合、一般的な分類よりもこのルールを絶対に優先してカテゴリ分けしてください：
{learning_rules}

以下のJSON配列形式のみで返してください。説明文・コードブロックは不要です。

[
  {{
    "date": "YYYY-MM-DD形式（レシートに日付があればそれ、なければ今日）",
    "category": "以下から最も適切なものを1つ: 食費, 外食, 交通費, 光熱費, 通信費, 医療費, 日用品, 衣服, 娯楽, 教育, 保険, その他",
    "expense_type": "以下から1つ: 固定費, 変動費, 特別支出, 貯蓄",
    "amount": 支払総額（税込）を純粋な整数で指定,
    "memo": "店名や購入内容を簡潔に（例: イオン / 野菜・肉類）"
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

        if isinstance(parsed, dict):
            parsed = [parsed]

        # 【安全弁】万が一AIが￥マークやカンマを返してきた場合のクレンジング処理
        cleaned_entries = []
        for entry in parsed:
            if "amount" in entry:
                if isinstance(entry["amount"], str):
                    # 数字以外の文字（￥やカンマ）をすべて排除して数値化
                    num_str = re.sub(r"\D", "", entry["amount"])
                    entry["amount"] = int(num_str) if num_str else 0
                elif isinstance(entry["amount"], (int, float)):
                    entry["amount"] = int(entry["amount"])
            cleaned_entries.append(entry)

        return {"entries": cleaned_entries}

    except Exception as e:
        print(traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"サーバー内部エラー: {str(e)}")


app.mount("/static", StaticFiles(directory="static"), name="static")

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app:app", host="0.0.0.0", port=port)