# CRAFT環境向け Dual-DAG アーキテクチャ提案

## 0. 現状の実装ステータス

この文書は最終的に目指す Dual-DAG アーキテクチャを含む提案であり，現時点の実装はそのうち CRAFT 向け MVP を中心に進んでいる．

現状は，Director の発話を `ReportedClaim` として保持し，Builder の候補行動に support/conflict/required-evidence/confidence を付け，必要なら `CLARIFY` に倒すところまで実装済みである．また，`DualDAGRuntime` により `Epistemic DAG` と `Action Candidate DAG` のノード・エッジを CRAFT ローカルに保持し，過去の public claim/action を検索して action scoring に使う runtime retrieval も実装済みである．さらに，CRAFT の action candidate を VillagerAgent Task DAG 向け projection に写す橋渡しと，Minecraft の task/action/observation/claim を read-only Dual-DAG artifact として出力する最小 projection も実装済みである．

一方で，提案の中核である `Hypothesis` と `ResolvedFact` による知識解決はまだ部分実装である．`Hypothesis` node は unresolved/conflicting evidence から生成・更新され，`supports` / `conflicts_with` / `derived_from` / `requires_confirmation_from` の一部 epistemic edge も populated されるが，`ResolvedFact`，`resolved_by`，graph traversal による action unlock，clarification response 取り込みと再評価，および Minecraft 実環境での runtime decision support への接続は未実装である．

| 領域 | 現状 |
| --- | --- |
| Epistemic metadata | 実装済み．`ObservedFact`, `PublicFact`, `ReportedClaim`, provenance を保持する |
| Action candidate metadata | 実装済み．候補行動に support/conflict/required-evidence/confidence を付与する |
| Runtime graph object | 部分実装済み．`DualDAGRuntime` がノード・エッジ・snapshot・public retrieval を持つ |
| Gated clarification | 実装済み．confidence/conflict/required evidence/span uncertainty に基づき `CLARIFY` を選ぶ |
| Adaptive gated clarification | 実装済み．状態に応じて clarification threshold を調整する |
| Evidence-aware Builder prompting | 実装済み．public evidence summary を Builder prompt に追加する |
| Partial-information safety | 実装済み．hidden target/oracle/private view の prompt/artifact leakage を検査する |
| Hypothesis / ResolvedFact | 部分実装済み．`Hypothesis` は生成・更新されるが，`ResolvedFact` と解決処理はまだない |
| Epistemic edge population | 部分実装済み．observation/claim/hypothesis/action 間に一部 edge を張る |
| Clarification metrics | 実装済み．resolution count/rate，quality score，post-clarification progress delta を集計する |
| CRAFT Action Candidate DAG と Task DAG projection | 実装済み．CRAFT action candidate を read-only Task DAG projection に写す |
| Graph traversal unlock | 未実装．action state を traversal で `blocked` から `executable` にする処理はない |
| Minecraft Task DAG 接続 | 部分実装済み．Minecraft task/action/observation/claim の read-only artifact projection はあるが，実行時 decision support への接続はまだない |

したがって，現在の実装は「完全な知識解決 Dual-DAG」ではなく，「CRAFT 上で claim と action candidate を構造化し，根拠付き scoring，runtime retrieval，gated/adaptive clarification，artifact analysis を行い，VillagerAgent Task DAG / Minecraft artifact への最小 projection まで備えた Dual-DAG MVP」と位置づける．

## 1. 目的

CRAFT は，3人の Director がそれぞれ異なる部分観測しか持たず，誰も完成図全体を知らない部分情報協調ベンチマークである．この環境では，単に各 Director の発話を Builder が即座に行動へ変換するだけでは，未観測部分への思い込み，視点の取り違え，曖昧な指示による誤配置が発生しやすい．

本提案では，エージェントが持つ「何を知っているか」「何を推測しているか」「どの行動がどの知識に支えられているか」を明示的に分離して管理するため，2層式 DAG，すなわち Dual-DAG アーキテクチャを導入する．

中心となる考え方は以下である．

> 部分情報下の協調では，private observation を直接 action に変換するのではなく，まず不確実な信念を外部化し，高コストな曖昧性を通信によって解消し，十分な根拠が揃った行動のみを実行可能にするべきである．

## 2. 全体構成

システムは，相互に連携する2つの DAG から構成される．

- `Epistemic DAG`: 知識，不確実性，他者発話，仮説，解決済み事実を管理する認識論的 DAG
- `Action Candidate DAG`: Builder が取り得る候補行動と，それらを支える根拠・依存関係を管理する実行候補 DAG

CRAFT では Director は直接ブロックを置かないため，第2層は一般的な物理タスク DAG ではなく，まず Builder の候補行動を扱う `Action Candidate DAG` として実装する．Minecraft 実環境へ拡張する段階では，この層を VillagerAgent の既存 Task DAG に接続する．現時点では，CRAFT action candidate から Task DAG projection への橋渡しと，Minecraft task/action/observation/claim から read-only Dual-DAG artifact への projection まで実装済みであり，Minecraft 実行制御そのものへの graph traversal / unlock 統合は今後の対象である．

## 3. Layer 1: Epistemic DAG

### 3.1 役割

Epistemic DAG は，各 Director が持つ private observation，public message，Builder action，他者発話，および未観測部分に関する仮説を管理する．目的は，LLM の暗黙の推測や思い込みを外部グラフ上に明示し，後続の行動判断や通信判断に利用できる形へ変換することである．

### 3.2 ノード種別

#### ObservedFact

Director が自分の private view から直接観測した情報．

例:

- D1 の bottom-left に yellow small が見える
- D3 の top row が blue small で構成されている

これは自分の projection 内では高信頼だが，3D 全体を意味するものではない．

#### PublicFact

全員が共有できる public history 由来の情報．

例:

- Builder が `(0,0)` layer `0` に `ys` を置いた
- D2 が前ターンで bottom-middle に green を見たと発話した

Builder action は環境状態として観測可能なため，行動計画に直接使いやすい．

#### ReportedClaim

他 Director の発話内容を表すノード．重要なのは，他者の発話は世界の真実ではなく，「その Director がそう主張した」という provenance 付き情報として扱う点である．

例:

- D2 claims: bottom-left は yellow である
- D3 claims: right wall の top row は blue である

ReportedClaim はそのまま Fact 化しない．他の観測や Builder action と整合した場合にのみ，行動判断に使える ResolvedFact へ近づく．

#### Hypothesis

未観測部分や他者視点に関する仮説．

例:

- D1 の left cell と D2 の left cell は同じ global coordinate を指している可能性が高い
- D3 の発話から，右壁側に blue の縦積みが存在する可能性がある

Hypothesis は confidence，expected gain，mistake cost を持ち，曖昧な行動を実行するか，通信で確認するかの判断に使われる．

#### ResolvedFact

複数の根拠により，行動判断へ使ってよい程度に解決された情報．

例:

- D1 と D2 の発話が一致し，かつ現在の board state と矛盾しないため，`(0,0)` に `ys` を置くのは妥当である
- Builder action により，`(0,0)` layer `0` はすでに `ys` で埋まっている

ResolvedFact は「絶対的な真実」ではなく，「現在の public evidence のもとで行動に使える確度を持つ情報」として扱う．

### 3.3 エッジ種別

- `supports`: あるノードが別のノードを支持する
- `conflicts_with`: ノード間に矛盾がある
- `derived_from`: Hypothesis がどの Fact/Claim から生成されたかを示す
- `resolved_by`: Hypothesis がどの観測や発話によって解決されたかを示す
- `requires_confirmation_from`: どの Director に質問すれば解決できるかを示す

### 3.4 スコア

各 Hypothesis または行動に関わるノードは，以下のスコアを持つ．

- `confidence`: 仮説の尤度
- `expected_progress_gain`: 正しければ進捗にどれだけ寄与するか
- `mistake_cost`: 間違えた場合の修正コスト
- `clarification_cost`: 質問・通信による遅延コスト
- `conflict_count`: 他ノードとの矛盾数

これにより，LLM の推測を単なる自然言語ではなく，通信・実行判断に使える構造化データとして扱える．

## 4. Layer 2: Action Candidate DAG

### 4.1 役割

Action Candidate DAG は，Builder が取り得る候補行動と，その行動がどの知識・発話・仮説に支えられているかを管理する．CRAFT では物理行動を行うのは Builder であるため，この層は Director ごとの Task DAG ではなく，Builder の候補行動グラフとして始める．

### 4.2 ノード種別

#### PlaceBlock

ブロック配置候補．

例:

- `PLACE:ys:(0,0):0`
- `PLACE:bl:(1,0):0:(2,0)`

#### RemoveBlock

ブロック除去候補．

例:

- `REMOVE:(1,2):0`
- `REMOVE:(1,2):0:(2,2)`

#### Clarify

追加質問や確認を行う候補．

例:

- D2 に bottom-left の色を確認する
- D3 に large block の span を確認する

#### WaitForEvidence

十分な根拠がないため行動を保留する候補．

### 4.3 ノード状態

- `blocked`: 必要な情報が不足している
- `candidate`: 実行候補だが確度が十分ではない
- `executable`: 実行可能な候補
- `executed`: 実行済み
- `invalidated`: 新たな evidence により無効化された

### 4.4 アンロック条件

Action node は，以下のいずれかを満たしたときに `executable` になる．

- 必要な ResolvedFact が揃った
- 複数 Director の ReportedClaim が整合している
- Builder の current board state と矛盾しない
- mistake cost が低く，探索的に実行してもよい
- oracle candidate や simulation により物理的妥当性が確認されている

逆に，以下の場合は `Clarify` または `WaitForEvidence` が選ばれる．

- top action confidence が閾値未満
- support する claim と conflict する claim が拮抗している
- 間違えた場合の repair cost が高い
- large block の span が曖昧
- layer や coordinate の解釈が不明確

## 5. Gated Coordination

Dual-DAG の重要な機能は，通信を常に行うのではなく，不確実性とコストに基づいて通信をゲートすることである．

基本方針は以下である．

```text
ask_if = uncertainty * mistake_cost > clarification_cost
```

CRAFT 向けには，より実用的に以下の条件を用いる．

```text
ask_if = candidate_actions are ambiguous
      or top_action_confidence < threshold
      or expected_repair_cost > communication_cost
      or required span/coordinate/layer is unresolved
```

これにより，「いつ質問すべきか」を単なるプロンプト上のヒューリスティックではなく，仮説と行動コストに基づく明示的な意思決定として扱える．

## 6. 動的連携サイクル

1. 各 Director が private view から ObservedFact を抽出する．
2. Director の発話が PublicFact / ReportedClaim として共有される．
3. Epistemic DAG が ReportedClaim や ObservedFact から Hypothesis を生成する．
4. Builder は current board state と Epistemic DAG から Action Candidate DAG を構築する．
5. 各 action candidate に対して support/conflict/confidence/mistake cost を評価する．
6. 十分に根拠が揃った action は executable になる．
7. 根拠が不足している action は blocked になり，必要に応じて Clarify node を生成する．
8. Clarify による回答が ReportedClaim または ResolvedFact として Epistemic DAG に追加される．
9. 新たな evidence により Action Candidate DAG が更新され，行動がアンロックされる．

## 7. 研究としての強み

### 7.1 不確実性の外部化

従来，LLM の推測や思い込みは hidden chain-of-thought や自然言語応答の中に埋もれがちであった．本手法では，それらを Hypothesis node として外部グラフに明示し，confidence や mistake cost とともに管理する．

### 7.2 通信タイミングの合理化

他者にいつ質問するかを，固定ルールや単純なターン制ではなく，不確実性，行動価値，修正コスト，通信コストの比較として扱う．これにより，過剰な通信と無謀な実行の両方を抑制できる．

### 7.3 partial-information 制約との整合性

ObservedFact，ReportedClaim，Hypothesis，ResolvedFact を分けることで，他者発話を即座に世界事実として扱う危険を避けられる．これは CRAFT の partial-information 制約に適している．

### 7.4 既存 DAG 系研究との差別化

AgentNet などの分散 DAG 型フレームワークは，主にタスク分割や割り当てに DAG を利用する．一方，本提案は DAG を「知識の不確実性」と「行動候補の根拠管理」に用いる．これは，CRAFT が要求する情報非対称性，他者視点の推測，通信による曖昧性解消に特化した設計である．

## 8. 実装 MVP

最初から完全な Dual-DAG を実装するのではなく，以下の段階的 MVP から始める．

### Phase 1: Epistemic Metadata

- Director prompt/output から `claims`, `uncertainties`, `suggested_constraints` を抽出する
- 各 claim に `source_director`, `turn_index`, `confidence`, `provenance` を付ける
- hidden target structure は保存しない

現状: 概ね実装済み．Director の private view から `ObservedFact`，public history から `PublicFact`，Director 発話から `ReportedClaim` を作り，provenance と visibility を保持している．ただし `suggested_constraints` は独立した構造としては未実装で，発話中の keywords/uncertainty による軽量抽出に留まる．

### Phase 2: Action Candidate Metadata

- Builder の候補 action に対して support/conflict する claims を記録する
- oracle candidate または parsed candidate に `supported_by`, `conflicts_with`, `confidence` を付与する
- report に `claim_support_count`, `claim_conflict_count`, `action_confidence` を追加する

現状: 実装済み．`action_candidates.py` が oracle candidate または parsed action から `ActionCandidateNode` を生成し，`supported_by`, `conflicts_with`, `required_evidence`, `confidence` を付与する．runtime retrieval により過去の public claim/action も config-gated に scoring へ反映できる．

### Phase 3: Gated Clarification

- confidence が低い，または conflict が高い場合に `CLARIFY` を選ぶ
- clarification の発生回数と，その後の progress/fallback 変化を測定する

現状: 実装済み．`gating.py` が `min_action_confidence`, `max_conflict_count`, `clarify_on_required_evidence`, `clarify_on_large_block_span_uncertainty`, `clarification_cost`, `mistake_cost_weight` を使って gate 判断を行う．結果は normalized artifacts, report, Dual-DAG analysis, experiment summary で集計できる．

### Phase 4: Full Dual-DAG

- Epistemic DAG と Action Candidate DAG を明示的な graph object として実装する
- action unlock 条件を graph traversal として扱う
- Minecraft 実環境では Action Candidate DAG を VillagerAgent Task DAG に接続する

現状: 部分実装済み．`DualDAGRuntime` は CRAFT ローカルの graph object として，epistemic/action nodes，action support/conflict edges，hypothesis nodes，epistemic edges，snapshot，serialization，public graph retrieval を持つ．CRAFT action candidate と VillagerAgent Task DAG の read-only projection，および Minecraft task/action/observation/claim の read-only artifact projection も実装済みである．ただし `ResolvedFact`, `resolved_by`, action state transition, graph traversal unlock, clarification response ingestion, Minecraft runtime decision support への接続は未実装である．

次の実装対象は以下である．これらは GitHub Issue として分割して管理する．

- `Hypothesis` node の confidence/update/resolution lifecycle を定義・拡張する
- `ResolvedFact` node と `resolved_by` edge を Epistemic DAG 側に追加する
- `ResolvedFact` または public evidence threshold による action unlock 条件を graph traversal として実装する
- `Clarify` / `WaitForEvidence` を明示的な action candidate として graph に残す
- clarification response を `ReportedClaim` / `ResolvedFact` として取り込み，候補行動を再評価する
- Minecraft artifact projection を runtime decision support に接続する
- Director output から `suggested_constraints` を独立した構造として抽出する

対応する未実装 Issue は以下である．

| Issue | 対象 |
| --- | --- |
| #77 | `ResolvedFact` node と `resolved_by` edge を Epistemic DAG に追加する |
| #78 | `Hypothesis` confidence/update/resolution lifecycle を定義する |
| #79 | graph traversal による CRAFT action candidate unlock を実装する |
| #80 | clarification response を取り込み，action candidate を再評価する |
| #81 | Minecraft Dual-DAG artifact を runtime decision support に接続する |
| #82 | `Clarify` / `WaitForEvidence` を明示的な action candidate として保持する |
| #83 | Director output から `suggested_constraints` を構造化抽出する |

## 9. CRAFT で守るべき制約

- Director に hidden target structure を渡さない
- Director に oracle moves を渡さない
- 他 Director の raw private view を渡さない
- ReportedClaim を即座に ResolvedFact として扱わない
- Builder action は public state として扱う
- すべての Fact/Claim/Hypothesis に provenance を持たせる

## 10. まとめ

本アーキテクチャは，CRAFT における協調の本質を「行動生成」ではなく「部分情報下での知識解決」として捉える．Dual-DAG により，不確実な信念と実行可能な行動を分離し，通信によって高コストな曖昧性を解消しながら，根拠のある行動だけをアンロックする．

この設計により，VillagerAgent は単なる multi-agent prompt system ではなく，partial-information collaboration に特化した，知識駆動型の協調エージェントとして拡張できる．
