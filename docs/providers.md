# 分類方式の比較

2026年10月20日に公式資料を確認しました。価格はUSDです。下表の仕様確認と、このツールでの実画像評価は別です。同じ女子スポーツ静止画・同じ棄却条件で比較できる公式ベンチマークは確認できていません。モデル間の精度順位は不明です。

| 方式 | 画像・構造化出力 | 費用 | 大量処理・未分類 | Macへの負荷 |
|---|---|---|---|---|
| TypeSafe Jev 1.13/1.14 | PNG/JPEG画像入力、typed choice、確率分布 | 入力100万トークン$0.042、出力無料。画像トークン推定式は未公表 | choiceに未分類を含め、confidenceでレビュー振り分け可能。画像入力は実験機能 | クラウド推論。ローカル前処理と通信の負荷は未測定 |
| Apple Vision | 固定カテゴリとconfidence。対応カテゴリは実機で列挙 | 外部API呼び出し費用なし | 1枚ずつ処理してSQLiteへ保存。競技と用具などの文脈を照合し、弱い候補は棄却 | ネイティブ処理。実測はローカル評価レポートで区別 |
| Gemini Vision | 画像入力、JSON Schema | 3.5 Flash-Lite標準は入力$0.30、出力$2.50/100万トークン。Batchは半額 | Batch、構造化された未分類ラベルに対応。生成スコアの校正は未確認 | クラウド推論。ローカルは画像の前処理と通信 |
| OpenAI Vision | 画像入力、Structured Outputs | GPT-6 Luna標準短文脈は入力$0.10、出力$0.50/100万トークン。Batchは半額 | Batch。スキーマに未分類を含められるが、内容の正確さは保証されない | クラウド推論。前処理と通信の負荷は未測定 |
| CLIP ViT-B/32 | 画像と英語の競技説明の類似度。Python側でJSON化 | 外部推論料金なし。初回の重み取得とローカル計算が必要 | 固定候補・競合候補差・私服や表彰式等の棄却候補。類似度を正解確率として扱わない | CPUスレッド1本、逐次処理。生成VLMではないがVisionより重み読み込みが必要 |

Jevを実験機能だけで除外しませんでした。画像仕様は各辺280～1568px、最大16MiB/画像で、`TypeSafe-Danger-Zone-Images: true` が必要です。詳細は[画像仕様](https://docs.typesafe.ai/images)、[モデルと価格](https://docs.typesafe.ai/models)、[confidence](https://docs.typesafe.ai/confidence)をご覧ください。

Appleの[VNClassifyImageRequest](https://developer.apple.com/documentation/vision/vnclassifyimagerequest)は任意の競技名を質問できるVLMではありません。一般カテゴリからの競技判定には限界があります。利用するOSとrevisionを結果のfingerprintに含めます。

Geminiは[画像入力](https://ai.google.dev/gemini-api/docs/image-understanding)、[構造化出力](https://ai.google.dev/gemini-api/docs/structured-output)、[価格](https://ai.google.dev/gemini-api/docs/pricing)、[Batch](https://ai.google.dev/gemini-api/docs/batch-api)を確認しました。無償サービスへの個人情報等の送信制限は[規約](https://ai.google.dev/gemini-api/terms)を確認してください。

OpenAIは[モデル仕様と価格](https://developers.openai.com/api/docs/models/gpt-6-luna)、[画像入力](https://developers.openai.com/api/docs/guides/images-vision)、[構造化出力](https://developers.openai.com/api/docs/guides/structured-outputs)、[Batch](https://developers.openai.com/api/docs/guides/batch)、[データ保持](https://developers.openai.com/api/docs/guides/your-data)を確認しました。

CLIPは[公式実装](https://github.com/openai/CLIP)、[公式モデルカード](https://github.com/openai/CLIP/blob/main/model-card.md)、[配布モデル](https://huggingface.co/openai/clip-vit-base-patch32)を確認しました。UCF101等の評価は今回の静止画分類精度ではありません。モデルカードは用途固有の検証を求めています。本ツールではローカル比較候補とし、人物の同定・属性推定には使いません。

## 実装した方式と追加方式

ネイティブVision、小型CLIP、明示した承認と費用上限を必要とするGeminiアダプタを実装しています。Jev・OpenAIの専用アダプタは未実装です。`module:factory` または明示した `command` アダプタで追加できます。比較表の画像対応を確認したことと、実測済みであることは区別してください。

Gemini 2.5 Flash-Liteの公式価格表には入力$0.10・出力$0.40/100万トークンの掲載がありますが、動作確認で新規ユーザーには利用不可というAPI応答がありました。モデル一覧への掲載だけではアカウントから利用できる証拠になりません。現在の実装は応答で案内された3.5 Flash-Liteを固定し、上表の料金で費用を予約します。

CLIPの閾値0.26と候補間差0.025、Visionの閾値は初期の保守的なヒューリスティックです。人手正解で校正済みとは扱いません。CLIPは224px中央クロップを含むため、小さい用具や画面端の情報を落とす場合があります。認識結果を自動で正解ラベルへ昇格させません。
