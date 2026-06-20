from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional
import gspread
from google.oauth2.service_account import Credentials
import os
import json

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
    
    # C列（3列目）のデータをすべて取得して、最初の空白行を探す
    col_c_values = sheet.col_values(3)
    
    # 2行目からチェックを開始
    target_row = 2
    for i, val in enumerate(col_c_values[1:], start=2):
        if not val or val.strip() == "":
            target_row = i
            break
    else:
        # 空白がなければ、現在のC列のデータ末尾の次の行
        target_row = len(col_c_values) + 1

    # 【変更】C列〜G列の範囲を指定（C列から右に5列分）
    range_name = f"C{target_row}:G{target_row}"
    row_data = [entry.date, entry.category,entry.memo, entry.expense_type, entry.amount]
    
    # 指定したC〜G列にデータを書き込む
    sheet.update(range_name, [row_data], value_input_option="USER_ENTERED")
    
    return {"status": "ok", "message": "記録しました"}


@app.get("/history")
def get_history(limit: int = 10):
    """直近の記録（C列からG列を読み込み）を新しい順で返す"""
    sheet = get_sheet()
    all_values = sheet.get_all_values()

    # ヘッダー行を除く
    data_rows = all_values[1:] if all_values else []

    rows = []
    # 各行から「C列(インデックス2)からG列(インデックス6)」を抜き出す
    for row in data_rows:
        # そもそもC列までデータが存在しない行はスキップ
        if len(row) <= 2:
            continue
            
        # C列（row[2]）が空っぽの行はデータ無しとみなしてスキップ
        if not row[2] or row[2].strip() == "":
            continue
            
        # C列から右側のデータを切り出し、足りない列があれば空白で埋める（最大5列）
        c_to_g_data = row[2:7]
        while len(c_to_g_data) < 5:
            c_to_g_data.append("")
            
        rows.append({
            "date":         c_to_g_data[0], # C列
            "category":     c_to_g_data[1], # D列
            "expense_type": c_to_g_data[3], # E列
            "amount":       c_to_g_data[4], # F列
            "memo":         c_to_g_data[2], # G列
        })

    # 有効なデータの中から、末尾からlimit件取得して新しい順に並べる
    recent = rows[-limit:][::-1]

    return {"rows": recent}


app.mount("/static", StaticFiles(directory="static"), name="static")
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)