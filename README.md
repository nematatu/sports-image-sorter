# Sports image sorter

画像の視覚的な内容からスポーツ競技の候補を判定し、競技別フォルダへコピーするCLIです。推論結果をローカルWeb画面で確認・修正できます。原本を移動・削除する機能はありません。

分類精度は利用する画像で検証してください。スコアは正解率ではありません。正解ラベルがなければ精度は算出しません。人の名前・顔・容姿から競技を推測する機能は実装していません。

## セットアップ

Python 3.11以降を使います。Apple VisionにはmacOSとSwiftコンパイラが必要です。必要な場合はAppleのCommand Line Toolsをインストールしてください。

```sh
git clone https://github.com/nematatu/sports-image-sorter.git
cd sports-image-sorter
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

以下の入力パスは例です。テスト対象として許可した画像フォルダだけを指定してください。入力、出力、台帳は互いに分離します。

## ローカルで分類してコピーする

```sh
sports-sorter --db runtime/state.sqlite scan ~/Pictures/sports-test
sports-sorter --db runtime/state.sqlite classify --engine vision --revision 2
sports-sorter --db runtime/state.sqlite summary
sports-sorter --db runtime/state.sqlite copy ~/Pictures/sports-sorted --dry-run
sports-sorter --db runtime/state.sqlite copy ~/Pictures/sports-sorted
sports-sorter --db runtime/state.sqlite verify ~/Pictures/sports-sorted
sports-sorter --db runtime/state.sqlite export runtime/results.json
```

`scan` は画像のSHA-256を登録します。`classify` は1枚ごとに結果を確定し、同じエンジン設定の処理済み画像を再利用します。`copy` は既存ファイルを上書きせず、衝突時に別名を採用します。`verify` は原本のハッシュ・サイズ・更新日時とコピーのハッシュを照合します。

入力の変更や消失を検出した場合は停止します。新しい画像を追加した場合は同じ `scan` を再実行できます。既存の画像内容を差し替えた場合は新しい台帳を用意してください。シンボリックリンクは拒否します。ディレクトリの権限エラーを「画像0件」として扱いません。

原本を読む際にOSがアクセス時刻を更新する可能性はあります。画像内容と更新日時を変更する処理は行いません。

## 画面で確認・修正する

```sh
sports-sorter --db runtime/state.sqlite serve --port 8765
```

[レビュー画面](http://127.0.0.1:8765)を開きます。競技・未分類・モデル不一致・未レビュー・エラーで絞り込み、サムネイルを押すと拡大できます。画像ごとのチェック、または「このページを選択」で最大48枚を選び、一括でラベルを保存します。

「人が画像を確認した正解ラベルとして保存」は、人が独立に確認した場合だけオンにしてください。初期状態はオフです。モデルの推論やアシスタントの暫定確認を正解データに昇格させないでください。

修正後は `copy` を再実行します。以前の分類先にあるコピーは自動削除しません。`verify` の `stale_copies` で確認できます。原本と既存出力を保護するための動作です。Web UIは台帳の修正だけを行います。

CLIでも修正できます。

```sh
sports-sorter --db runtime/state.sqlite review 1 2 --label 未分類 --evidence '競技用具と会場を特定できません'
sports-sorter --db runtime/state.sqlite duplicates
```

## エンジンを切り替える

### Apple Vision

`--engine vision --revision 1` と `--revision 2` を比較できます。実機で対応revisionを確認し、非対応ならエラーにします。分類器は一般カテゴリを返すため、競技候補のスコア・競合候補・用具や会場の補助情報で保守的に判定します。表彰式などの候補は棄却します。OSや設定の違いもfingerprintに含めます。

### 小型CLIP

生成VLMを使わない任意のローカル比較方式です。依存パッケージとモデル重みの取得が必要です。

```sh
python -m pip install -e '.[clip]'
python -m sports_sorter.clip_engine download
sports-sorter --db runtime/state.sqlite classify --engine clip
```

`openai/clip-vit-base-patch32` の固定リビジョンを使い、推論時はオフライン・CPUスレッド1本・1枚ずつ処理します。`SPORTS_CLIP_MODEL` でモデル保存先を変更できます。類似度の閾値は校正済みではありません。中央クロップで小さい用具を失う場合があります。標準セットアップではPyTorchを導入しません。

### Gemini

Gemini 3.5 Flash-Liteを使う専用アダプタです。実画像の送信・有料利用について、画像の所有者から承認を得た後に実行してください。

```sh
cp .env.example .env
chmod 600 .env
# .env の GEMINI_API_KEY をエディタで設定します。
python -m sports_sorter.cloud --db runtime/state.sqlite \
  --env-file .env --budget-db runtime/api-budget.sqlite \
  --max-usd 1 --allow-external --provider gemini --dry-run
python -m sports_sorter.cloud --db runtime/state.sqlite \
  --env-file .env --budget-db runtime/api-budget.sqlite \
  --max-usd 1 --allow-external --provider gemini
```

送るのは長辺最大1024pxに縮小し、メタデータを除去して生成したJPEGです。ファイル名・元のパス・EXIFは送りません。場面種別がスタジオ撮影・顔アップ・表彰式・私服・判定不能の場合は、競技スコアが高くても未分類にします。

標準料金の入力$0.30・出力$2.50/100万トークンを使います。1リクエストにつき入力32,000・出力512トークン分、$0.01088を送信前にSQLiteで予約します。トークン計算APIで入力上限を確認してから生成APIを呼びます。失敗・タイムアウトの予約も解放せず、結果不明の自動再送を拒否します。成功した同一ピクセル・設定の結果は再利用します。

この費用上限は、記載料金とAPIのトークン上限が守られることを前提にした保守的な制御です。請求書やアカウント側の予算設定の代わりではありません。料金変更時はコードの料金定数も更新してください。同じ処理では同じ費用台帳を保持し、別台帳を作って上限を回避しないでください。

本アダプタは逐次API処理です。Batch APIは未実装です。大量処理は中断再開・成功キャッシュ・費用上限で管理できますが、APIのレート制限による失敗は手動確認が必要です。数千枚の外部実測は未実施です。

### 独自エンジン

`--engine package.module:factory` は引数なしのfactoryを読み込みます。返すオブジェクトは `fingerprint: str` と `classify(PIL.Image) -> Prediction` を持ちます。`Prediction` は `sports_sorter.core` からimportできます。入力は向き補正・RGB化・最大1024px・メタデータ除去済みのピクセルです。

`--engine command --command 'python adapter.py' --model-id stable-v1` はJSONを標準入力に渡し、JSONの `label`, `score`, `evidence`, 任意の `raw` を受け取ります。一時画像へのパスを受け取るため、信頼するコマンドだけを指定してください。独自アダプタの通信・課金は運用者が管理します。

JevとOpenAIは[公式仕様の比較](docs/providers.md)を掲載しています。専用アダプタは未実装です。

## 精度を検証する

同じ台帳で複数エンジンを実行すると予測が別々に残ります。`summary --engine-key KEY`、`copy --engine-key KEY`、`verify --engine-key KEY` で比較対象を指定できます。UIを固定エンジンで開く場合は `python -m sports_sorter.server --db runtime/state.sqlite --engine-key KEY` を使います。

正解は画像を独立に確認してから登録してください。`review ... --human-verified` で登録できます。`summary` は評価対象数・正解数・精度・評価対象のカバー率・推論失敗数を返します。正解がなければ `accuracy.value` は `null` です。推論エラーを「未分類の正解」として数えません。レビューした標本の精度は、全画像の精度とは限りません。

完全重複はSHA-256で検出します。dHashの近似重複は確認候補であり、同一画像の証明ではありません。近似比較はO(N²)で、候補出力は1,000組までです。重複ファイルも元の各パスに対してコピーします。

## 対応形式とエラー

JPEG、PNG、WebP、BMP、静止GIF、TIFF、AVIF等をPillowで読み込みます。HEIC/HEIFは環境に対応デコーダがなければ読み込みエラーになります。アニメーションや複数ページ画像は自動判定しません。これらも台帳に記録し、未分類として元のバイト列をコピーします。デコーダの追加や一時障害解消後は `scan` と `classify --retry` で再試行できます。

中断で残った不完全コピーは保持し、次回は別名で再コピーします。`verify` は必要な各画像の正常コピーの有無と、残存する破損・旧分類コピーの件数を別々に報告します。

## テストと公開前検査

```sh
python -m unittest discover -s tests -v
python scripts/make_demo.py runtime/demo --count 60
sports-sorter --db runtime/demo/state.sqlite serve --port 8766
python scripts/measure_local.py --db runtime/state.sqlite --engine vision
git diff --cached
git log --all --stat
python scripts/audit_public.py
```

デモ画像とラベルは手続き的な合成データです。競技分類の精度試験ではありません。CIはLinuxとmacOSで自動テストを実行し、macOSでは生成画像をVisionへ入力します。外部APIはCIから呼びません。

実画像・サムネイル・台帳・APIキー・個人パスを含む結果は公開しないでください。`runtime/`, `.audit/`, `.env` はGitの除外対象です。公開検査スクリプトはステージ済みファイルと全到達可能コミットを検査します。任意の個人情報を完全検出できるわけではないため、差分・履歴・作者情報も目視してください。Downloadsへのアクセスはコードで拒否します。

設計は[architecture](docs/architecture.md)、方式調査は[providers](docs/providers.md)、UIの参照先は[ui-references](docs/ui-references.md)をご覧ください。MITライセンスです。Apple Vision・CLIP・外部APIの利用条件は各提供元の条件も確認してください。
