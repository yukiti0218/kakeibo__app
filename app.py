
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
    row_data = [entry.date, entry.category, entry.expense_type, entry.amount, entry.memo]
    sheet.append_row(row_data, value_input_option="USER_ENTERED")
    return {"status": "ok", "message": "記録しました"}
 
 
@app.get("/history")
def get_history(limit: int = 10):
    """直近の記録を新しい順で返す"""
    sheet = get_sheet()
    all_values = sheet.get_all_values()
 
    # ヘッダー行を除く（1行目がヘッダーの場合）
    data_rows = all_values[1:] if all_values else []
 
    # 末尾からlimit件取得して新しい順に並べる
    recent = data_rows[-limit:][::-1]
 
    rows = []
    for row in recent:
        # 列が足りない場合に備えてpadding
        while len(row) < 5:
            row.append("")
        rows.append({
            "date":         row[0],
            "category":     row[1],
            "expense_type": row[2],
            "amount":       row[3],
            "memo":         row[4],
        })
 
    return {"rows": rows}
 
 
app.mount("/static", StaticFiles(directory="static"), name="static")
if __name__ == "__main__":
    import uvicorn
    # Renderが指定するポート番号（環境変数 PORT）を読み込み、無ければ8000番を使う
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)