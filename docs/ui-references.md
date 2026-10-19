# レビューUIの参照元

2026年10月20日に公式資料と公開スクリーンショットを確認しました。利用者数・評価点は独立に確認していません。「高評価」「利用者数が多い」という数値評価は行っていません。

| 参照先 | 確認した画面・操作 | 採用した構造 |
|---|---|---|
| [Label Studio Data Manager](https://labelstud.io/guide/manage_data)、[画面](https://labelstud.io/images/project/dm-community.png) | 上段のFilters/Order by/Actions、チェックボックス、画像付き一覧、リスト・グリッド切替 | 上段フィルタ、サムネイル、選択数、一括修正 |
| [Label Studio画像分類](https://labelstud.io/templates/image_classification) | 画像と分類選択肢の組み合わせ | 画像を拡大してからラベルを修正 |
| [CVATタスク一覧](https://docs.cvat.ai/docs/workspace/tasks-page/)、[画面](https://docs.cvat.ai/images/image006_detrac.jpg) | 検索・絞り込み、プレビューから個別レビュー | 一覧から拡大確認へ進む導線 |
| [CVAT一括操作](https://docs.cvat.ai/docs/workspace/bulk-actions/) | 現在ページだけを全選択、ページ変更で選択解除 | 「このページを選択」と範囲を表示。48枚ごとのページング |
| [CVAT Consensus](https://docs.cvat.ai/docs/qa-analytics/consensus/) | 一致度の低い注釈を絞り込んで確認 | モデル不一致専用フィルタへの応用 |

推論結果、目視修正、人が確認した正解ラベルを区別します。正解チェックは初期状態でオフです。サムネイルは遅延読み込みし、サーバーのメモリキャッシュを128枚に制限します。数千枚すべての画像をブラウザへ読み込みません。構造化されたモデル比較の根拠は拡大画面に表示します。
