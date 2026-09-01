AQS100 Alpaca → Telegram 監控

這個小程式只做一件事：定期讀取 Alpaca 最新行情，按照已完成回測的策略檢查進場和出場訊號，然後把訊號、參考價格和 Portfolio 損益傳到 Telegram。它不會下單，不會連接 IBKR，也沒有 Paper 或 Live Trading 功能。

目前使用的策略

設定檔 strategies.json 目前包含最後跨 ticker Portfolio 的 7 個策略：CRWD 5 個、HWKN 1 個、KO 1 個。每個策略的 Target、Feature、Model、Strategy、length、threshold 和權重都固定寫在設定檔中。監控程式不會重新執行 Grid Search。

10,000 USD 資金計算

initial_capital_usd 已設定為 10,000 USD。7 個策略目前都是等權重，因此每個策略的參考資金為 10,000 ÷ 7 = 1,428.57 USD。出現 ENTRY 時，參考股數為 floor(1,428.57 ÷ 訊號價格)，也就是只取整股並向下捨去；這只是研究參考，不會自動下單。若價格是 112 USD，參考股數就是 12 股。

程式會把每個策略的百分比損益乘以該策略的參考資金，換算成美元；Portfolio 美元損益則以 10,000 USD 為基準。這是按照策略訊號計算的理論損益，不是券商帳戶實際成交損益，也未包含滑價、未成交、股息或稅務差異。

資料使用 Alpaca Market Data，interval 為 1 小時、feed 為 IEX，波動率欄位使用 VIXY 作為代理，不是真正的 VIX。程式會等完整的 1 小時 K 線結束後才判斷。

Telegram 訊息

通知已改成分區排版，方便快速監控。訊息會依序顯示訊號事件、股票、策略、時間、訊號價格、參考股數、配置資金、Portfolio 損益和各策略損益。實際格式類似：

Plain Text


【AQS100 訊號通知】
===================

動作         ：ENTRY
股票         ：KO
策略         ：robust_scaler / trend_reverse_long / L80 / T1.35
時間         ：2026-08-31 12:00:00
訊號價格     ：$89.0600
參考股數     ：15 股
策略配置資金 ：$1,428.57

【Portfolio 狀態】
起始資金       ：$10,000.00
累積損益       ：+1.25%（$+125.00）
今日損益       ：+0.30%（$+30.00）

【各策略狀態】
KO｜trend_reverse_long｜robust_scaler
  損益：+1.10%（$+15.71）｜參考股數：15 股



有新 ENTRY 或 EXIT 時才傳送訊號通知；每天美東收市後最多傳送一份每日摘要。第一次執行只建立狀態，不會把歷史資料誤當成即時損益，也不會發假訊號。

「參考價格」是觸發訊號的已完成 K 線收盤價，不是保證成交價。實際成交價格可能不同。

GitHub Actions 所需 Secrets

在 GitHub repository 的 Settings → Secrets and variables → Actions → New repository secret 中建立以下四個 Secrets：

•
ALPACA_API_KEY

•
ALPACA_SECRET_KEY

•
TELEGRAM_BOT_TOKEN

•
TELEGRAM_CHAT_ID

不要把這四個值寫進程式碼或公開貼在聊天裡。Workflow 每 5 分鐘執行一次；GitHub 排程可能延遲，所以它不是秒級即時監控。

執行方式

Workflow 可以在 GitHub Actions 頁面手動按 Run workflow 測試。現在不需要選任何 true/false：只要是手動執行，就會自動傳送一則 [AQS100 Telegram 測試成功]，用來確認四個 Secrets 和 Telegram 發送功能；自動排程則不會傳送這則測試訊息。這個測試只發通知，不會下單。

正常輸出會顯示 status=OK；沒有新訊號時不會傳一般 Portfolio 訊息。程式會把 state.json 提交回 repository，用來記住上一根已處理 K 線、各策略目前訊號和累積損益，避免重複通知。

這是通知與研究用途，不是投資建議，也不代表歷史回測結果會在未來重現。

