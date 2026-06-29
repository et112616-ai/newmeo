# Stock LINE Bot v1：Render + Make 4 Module 設定

## 1. Render

建議 Python 版本：`3.12.11`

Render Environment Variables：

```text
PYTHON_VERSION=3.12.11
IMAGE_URL=https://你的可用圖片網址
FINMIND_TOKEN=你的 FinMind token，可先空白
```

Start Command：

```text
gunicorn app:app
```

測試健康檢查：

```text
GET https://你的render網址/
```

應回：

```json
{"status":"ok","service":"stock-line-bot"}
```

---

## 2. Make 4 Module

### Module 1：LINE - Watch Events

照原本 LINE Official Account webhook 觸發。

---

### Module 2：HTTP - Make a request

Method：`POST`

URL：

```text
https://你的render網址/get_chart
```

Headers：

```text
Content-Type: application/json
```

Body type：Raw
Content type：JSON / application/json

Body：

```json
{
  "events": {{toJSON(1.events)}}
}
```

如果 Make 不接受 `toJSON(1.events)`，請改用 Mapping 面板插入 LINE Watch Events 的完整 events 陣列。

Parse response：建議 `Yes`。

---

### Module 3：Parse JSON

如果 HTTP Module 已經 `Parse response = Yes`，這顆可以省略；但若保留，Content 填：

```text
{{2.body}}
```

Schema：

```json
{
  "type": "object",
  "properties": {
    "messages": {
      "type": "array"
    }
  },
  "required": ["messages"]
}
```

---

### Module 4：LINE - Reply Message / Make an API Call

Reply Token：

```text
{{1.events[1].replyToken}}
```

如果你的 Make 顯示 index 從 0 開始，使用：

```text
{{1.events[0].replyToken}}
```

Messages：

若有 Parse JSON Module：

```text
{{3.messages}}
```

若 HTTP Module 已 Parse response 且沒有第 3 顆 Parse JSON：

```text
{{2.messages}}
```

不要使用：

```text
{{2.body}}
{{2.data}}
[{{3.messages}}]
```

因為 messages 必須是「陣列」，不能是字串，也不能再包一層陣列。

---

## 3. 測試文字

在 LINE 輸入：

```text
2330
```

也可試：

```text
台積電
華通
```

中文名稱查詢使用 `twstock` 做名稱轉代號，再用 yfinance 抓價格。

---

## 4. 第一版功能狀態

已完成：

- 文字輸入股票代號 / 名稱
- 按鈕 Postback 狀態解析
- 即時走勢
- K 線
- 法人籌碼圖（FinMind 可用時嘗試抓；失敗 mock fallback）
- 大戶表格（mock fallback）
- 融資券表格（mock fallback）
- 期貨按鈕保留並回提示文字
- 固定輸出 `{ "messages": [...] }`

未完成 / 第二版：

- 真正上傳 Matplotlib 圖片到 Cloudinary / S3
- 集保大戶正式接資料
- 融資券正式接 FinMind
- 台灣個股期貨正式資料源
