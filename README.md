# meetingTranslater

線上會議即時字幕翻譯：把 Teams / Zoom / Google Meet 的英文語音即時變成繁體中文字幕，全部在本機 Mac 上執行，會議內容不會離開你的電腦。

> **English:** Local, private real-time subtitles for online meetings on macOS. System audio (via a BlackHole loopback device) → ffmpeg → energy-based VAD → whisper.cpp (English ASR on Apple GPU) → Ollama LLM (translation to Traditional Chinese) → terminal, an always-on-top browser overlay, and a saved transcript. Standard-library Python only.

```
會議軟體聲音 ──► 多重輸出裝置 ──► 耳機（你照常聽）
                      └─────────► BlackHole 2ch ──► ffmpeg ──► 切句（VAD）
                                                     ──► whisper.cpp 辨識英文（Apple GPU）
                                                     ──► Ollama 翻譯成繁體中文
                                                     ──► 終端機字幕 ＋ 瀏覽器置頂字幕 ＋ 逐字稿檔
```

## 特色

- **全程離線、私密**：語音辨識與翻譯都在本機，不經過任何雲端服務。
- **延遲低**：一句話說完後約 2 到 4 秒出現中文；中文逐字浮現，不必等整句。
- **置頂浮動字幕**：瀏覽器頁面一鍵開成永遠在最上層的小視窗，拖到會議畫面上方。
- **逐字稿自動存檔**：英文原文加中文翻譯，`.txt`（與終端機畫面相同的排版）與 `.jsonl` 各一份，會後可丟給 LLM 做摘要。
- **詞彙表**：會前加入專案名、人名、縮寫，辨識更準、譯法一致。
- **零 Python 依賴**：`live_subs.py` 只用標準函式庫，不用裝任何 pip 套件。

## 需求

| 項目 | 說明 |
|------|------|
| macOS 13 以上、Apple Silicon | whisper.cpp 用 Metal 加速；Intel Mac 可跑但較慢 |
| [Homebrew](https://brew.sh) | 安裝下列工具 |
| ffmpeg | 讀取音訊裝置並轉成 16 kHz 單聲道 |
| whisper.cpp | 提供 `whisper-server`，語音辨識 |
| [Ollama](https://ollama.com) | 跑翻譯用的 LLM |
| BlackHole 2ch | 虛擬音訊裝置，把系統聲音複製一份給程式 |
| Google Chrome（選用） | 置頂浮動字幕用到 Chrome 116 以上的 Document Picture-in-Picture |

## 安裝

```bash
brew install ffmpeg whisper-cpp
brew install --cask ollama blackhole-2ch      # BlackHole 安裝時會要求管理者密碼

git clone https://github.com/ShibaYui/meetingTranslater.git
cd meetingTranslater
./models/download.sh                          # whisper large-v3-turbo，1.6 GB（或 ./models/download.sh q5_0 抓 574 MB 版）
ollama pull qwen3:8b                          # 翻譯模型；可換成任何你偏好的模型

cp config.env.example config.env              # 想換模型或 port 時編輯它
cp glossary.example.txt glossary.txt          # 會議詞彙表，依每場會議修改
```

翻譯模型可以是一般的對話 LLM（如 `qwen3:8b`），也可以是翻譯專用模型（Hy-MT2、TranslateGemma），後者小很多也快很多；實測比較與切換方式見下方「翻譯模型怎麼選」。Qwen3 系列的思考模式程式會自動關閉。

## 一次性音訊設定

### 1. 建立「多重輸出裝置」

有 Xcode 或 Command Line Tools 的話，一行指令就能建立並設為預設輸出（會把你目前在用的喇叭或耳機加上 BlackHole 2ch 綁在一起）：

```bash
swift tools/create-multi-output.swift            # 建立並設為預設輸出
swift tools/create-multi-output.swift --remove   # 不要了就刪掉
```

手動做法：

1. 打開「音訊 MIDI 設定」（Spotlight 搜尋 Audio MIDI Setup）。
2. 左下角「+」→「建立多重輸出裝置」。
3. 勾選你的耳機或喇叭，**再勾選 BlackHole 2ch**。
4. 「主要裝置」選耳機；BlackHole 那一列勾「偏移修正」（Drift Correction）。
5. 對這個多重輸出裝置按右鍵 →「使用此裝置作為聲音輸出」。

多重輸出裝置下鍵盤音量鍵會失效，音量請在此視窗調整耳機那一列。另外，送進 BlackHole 的聲音會跟著喇叭／耳機的音量一起縮放（實測喇叭音量 38% 時衰減 24.5 dB），所以開會時不要把喇叭調到接近靜音，否則字幕會收不到聲音。

### 2. 會議軟體

- **Teams**：設定 → 裝置 → 喇叭選「多重輸出裝置」（或維持「系統預設」）。
- **Zoom**：設定 → 音訊 → 喇叭選「多重輸出裝置」。
- **Google Meet**：使用系統預設輸出即可。

麥克風維持原本的麥克風，**不要**選 BlackHole，否則對方會聽不到你。

**換耳機時要重建多重輸出裝置**：多重輸出裝置綁的是建立當下的喇叭或耳機。之後若改戴 EarPods、AirPods 或其他耳機，請在接上耳機後執行 `./run.sh route "EarPods"`（名稱用耳機在系統裡顯示的名稱），再到會議軟體確認喇叭仍選多重輸出裝置。若會議軟體直接選了耳機本身，聲音就不會經過 BlackHole，字幕會收不到。

### 3. 麥克風權限

第一次執行時 macOS 會詢問終端機 App 的麥克風權限，請允許。BlackHole 雖是虛擬裝置，macOS 仍視為錄音來源。

## 使用

```bash
./run.sh start
```

它會依序：啟動 `whisper-server`、把翻譯模型載入記憶體、監聽 BlackHole，並開啟 `http://127.0.0.1:8765/`。

瀏覽器頁面：

- **浮動置頂視窗**：開一個永遠在最上層的小視窗，拖到會議畫面上方。關掉它字幕會移回分頁。
- **A− / A+**：調整字體大小（鍵盤 `-` / `+` 亦可）。

終端機同時顯示英文原文、中文翻譯與每句耗時；`Ctrl-C` 結束。逐字稿存在 `transcripts/日期-時間.txt`（排版同終端機：時間、英文、中文、每句耗時）與 `.jsonl`（結構化資料）。舊版留下的 `.jsonl` 可用 `python3 tools/export_transcript.py transcripts/日期-時間.jsonl` 轉成 `.txt`。

### 其他指令

```bash
./run.sh test                     # 用 macOS 合成語音跑完整流程，不需 BlackHole，驗證安裝是否成功
./run.sh devices                  # 列出音訊輸入裝置
./run.sh warm                     # 會前先把翻譯模型載入記憶體
./run.sh stop                     # 關閉 whisper-server
./run.sh route "EarPods"          # 換耳機後重建多重輸出裝置（名稱用系統顯示的耳機名）
./run.sh start --vu               # 顯示即時音量與門檻，用來調靈敏度
./run.sh start --no-translate     # 只要英文字幕
./run.sh start --device "MacBook Pro Microphone"   # 沒有 BlackHole 時用麥克風收喇叭聲音
python3 live_subs.py --help       # 全部參數
```

### 會後摘要

```bash
./summarize.sh transcripts/20260101-0900.txt
```

用 [Claude Code](https://claude.com/claude-code) 的非互動模式讀逐字稿，依 `prompts/summarize.txt` 產出重點、決策、待辦、未定案問題與「辨識可疑段落」，寫到同名 `-summary.md`。沒有 Claude Code 的話，把 `prompts/summarize.txt` 與逐字稿貼給任何 LLM 即可。

## 翻譯模型怎麼選（實測）

程式依模型名稱自動選用提示詞格式（`--prompt-style auto`）：名稱含 `translategemma` 用 TranslateGemma 的固定模板，含 `hy-mt` 用 Hy-MT2 的模板（並把 `glossary.txt` 當成「參考翻譯」帶進去），其他一律當一般對話模型。翻譯專用模型輸出若夾雜簡體字，裝了 OpenCC（`brew install opencc`）就會自動用 `s2t` 轉成繁體。

以 7 句含行話的會議英文（driver regression、release branch was cut、take this offline、won't have bandwidth…）在 Apple Silicon 上人工逐句判定語意，單次抽樣：

| 模型 | 下載 | 每句平均 | 語意正確 | 備註 |
|------|------|----------|----------|------|
| Qwen3 27B 類一般模型（4-bit） | 16 GB | 1.3 秒 | 7 / 7 | 最口語、最會用台灣用語，但需 32 GB 以上記憶體 |
| `hy-mt2:7b`（騰訊 Hy-MT2 7B） | 4.6 GB | 0.5 秒 | 6 / 7，加詞彙表後 7 / 7 | 性價比最好；「regression」會誤成「回歸分析」，詞彙表可修正 |
| `translategemma:12b` | 8.1 GB | 0.9 秒 | 6 / 7 | 忠實但句子偏長；模板固定，無法帶詞彙表 |
| `hy-mt2:1.8b` | 1.1 GB | 0.16 秒 | 6 / 7 | 超輕量；慣用語偶爾直譯，建議加 `--mt-prompt-lang zh` |
| `translategemma:4b` | 3.3 GB | 0.3 秒 | 4 / 7 | 技術行話常翻錯，不建議 |

整體延遲還包含停頓偵測 0.7 秒與語音辨識 0.5 到 0.9 秒，因此翻譯從 1.3 秒降到 0.5 秒時，體感約快 1 秒。

### 使用 Hy-MT2（建議的輕量選擇）

Ollama 直接跑 Hugging Face 上的 Hy-MT2 GGUF 時，自動轉出的對話模板是壞的（輸入根本送不進模型），請用本專案附的 Modelfile 建立：

```bash
ollama pull hf.co/tencent/Hy-MT2-7B-GGUF:Q4_K_M
ollama create hy-mt2:7b -f models/Modelfile.hy-mt2-7b
# 或超輕量版
ollama pull hf.co/tencent/Hy-MT2-1.8B-GGUF:Q4_K_M
ollama create hy-mt2:1.8b -f models/Modelfile.hy-mt2-1.8b
```

然後在 `config.env` 設 `OLLAMA_MODEL=hy-mt2:7b`，或臨時切換：

```bash
OLLAMA_MODEL=hy-mt2:7b ./run.sh start
OLLAMA_MODEL=translategemma:12b ./run.sh start    # ollama pull translategemma:12b
```

## 參數調整

| 參數 | 預設 | 說明 |
|------|------|------|
| `--threshold-db` | -60 | 判定為語音的最低音量（dBFS）。多重輸出裝置會把喇叭音量套用到 BlackHole 那一份，所以預設值很低；麥克風收音時由自適應噪音底線（底線 +15 dB）接手 |
| `--silence-ms` | 700 | 停頓多久算一句結束。太短會把一句切成兩半 |
| `--soft-cut-s` | 7 | 超過這個長度後，遇到 0.3 秒短停頓就切句 |
| `--max-seg-s` | 12 | 最長一句，超過強制切 |
| `--context-pairs` | 1 | 給一般對話模型看的前幾句對照，有助上下文一致；設 0 最快。翻譯專用模型不使用 |
| `--prompt-style` | auto | `chat` / `translategemma` / `hymt`，auto 依模型名稱判斷 |
| `--mt-prompt-lang` | en | Hy-MT2 指令語言；1.8B 用 `zh` 較能維持繁體 |
| `--opencc` | auto | 譯文後處理：`off` / `s2t` / `s2tw` / `s2twp`；auto 時翻譯專用模型用 `s2t` |
| `--web-port` | 8765 | 字幕網頁 port |

## 疑難排解

| 現象 | 處理 |
|------|------|
| 沒有任何字幕，`--vu` 顯示 -90 dB | 聲音沒進 BlackHole：確認系統輸出是多重輸出裝置、會議軟體喇叭設定正確 |
| ffmpeg 報 Input/output error 或權限錯誤 | 系統設定 → 隱私權與安全性 → 麥克風，允許你的終端機 App |
| 第一句翻譯等很久 | 模型剛載入；`run.sh start` 已先預熱，手動跑 `live_subs.py` 前可先 `./run.sh warm` |
| 翻譯出現簡體字或用語不對 | 在 `glossary.txt` 指定譯法；或換更大的模型 |
| 一句被切成兩半 | 加大 `--silence-ms`，例如 900 |
| 兩個人連著講被黏成一段 | 減小 `--soft-cut-s` |
| 想換辨識模型 | 在 `config.env` 設 `WHISPER_MODEL=models/ggml-large-v3-turbo-q5_0.bin` |
| whisper-server 起不來 | 看 `logs/whisper-server.log`；`./run.sh stop` 後重試 |

## 專案結構

| 檔案 | 說明 |
|------|------|
| `live_subs.py` | 主程式：音訊擷取、VAD 切句、呼叫 whisper-server 與 Ollama、終端機／網頁輸出、存檔 |
| `overlay.html` | 瀏覽器字幕頁，含置頂浮動視窗 |
| `run.sh` | 啟動器：`start` / `test` / `devices` / `warm` / `stop` / `route` |
| `models/download.sh` | 下載 whisper 模型 |
| `models/Modelfile.hy-mt2-*` | Hy-MT2 翻譯模型的 Ollama Modelfile（修正過的對話模板） |
| `tools/create-multi-output.swift` | 一鍵建立「喇叭 + BlackHole」多重輸出裝置並設為預設輸出 |
| `tools/export_transcript.py` | 把 `.jsonl` 逐字稿轉成終端機排版的 `.txt` |
| `summarize.sh`、`prompts/summarize.txt` | 會後摘要 |
| `config.env.example` | 設定範例（模型、port） |
| `glossary.example.txt` | 詞彙表範例 |

## 運作原理

1. **擷取**：ffmpeg 從 BlackHole 讀取系統聲音，轉成 16 kHz 單聲道 PCM 串流。
2. **切句**：以 20 ms 為單位計算音量，超過門檻視為語音；停頓 0.7 秒即成一句，長句在短停頓處切開，最長 12 秒。噪音底線會自動適應。
3. **辨識**：每句以 WAV 送到 `whisper-server`，過濾 `no_speech_prob` 偏高的片段與常見幻覺句（如 "Thank you."）。
4. **翻譯**：以串流方式呼叫 Ollama `/api/chat`，system prompt 要求台灣用語繁體中文並保留專有名詞；前一句的對照一併提供以維持上下文。
5. **顯示**：終端機即時列印；同時以 Server-Sent Events 推送到瀏覽器頁面；每句寫入逐字稿。

## 授權

MIT，見 [LICENSE](LICENSE)。
