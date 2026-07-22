# LaunchIt 開発メモ (for Claude Code)

ユーザー向けの機能説明は README.md 参照。ここは開発を再開する人(将来のセッション)向け。

## 構成

単一スクリプト `launchit.py`(~900行)。実設定は `launchit.json`(gitignore、
初回起動時に DEFAULT_CONFIG から生成)。ログは `launchit.log`(gitignore)。

### スレッド構造

- **Tk メインスレッド** — UI 全部。他スレッドからの操作は `cmd_queue` に文字列コマンドを積み、`_poll_queue`(80ms間隔の `after`)が処理。コマンド1つの例外でループが死なないよう個別 try/except
- **HotkeyListener** — RegisterHotKey + GetMessage ループ。登録はこのスレッド内でのみ行う(Win32の制約)。ホットキー変更は `PostThreadMessageW(WM_APP+1)` で再登録。パススルー時は unregister → keybd_event 合成 → 0.15s 待ち → re-register(即時再登録だと自分の合成キーをまた飲む)
- **IPC サーバー** — 127.0.0.1:48123。bind 失敗 = 多重起動(既存に show を送って終了)。コマンド: ping/show/hide/toggle/reload/rescan/refresh/editcfg/edititems/selfrestart/quit、`status`(JSON応答、IPC_HANDLERS でインライン処理)
- **pystray** — 別スレッド。Windows では左クリック1回で default メニュー項目が発火
- **ポートウォッチャー** — url/check_port 持ちの app 項目へ TCP 接続で稼働判定。表示中5s/非表示20s、`_port_kick` イベントで即時再スキャン

### 最近使ったフォルダビュー

検索欄が空のとき Space で main/recent ビューをトグル(`_toggle_view`)。
ホットキーは3状態サイクル(`toggle()`): 非表示→main→recent→非表示
(修飾キー押しっぱなしで Space 連打するとフォルダモードに入れる)。
`_recent_folders()` が shell Recent (`%APPDATA%\Microsoft\Windows\Recent\*.lnk`)
を IShellLinkW(ctypes COM 直叩き)で解決しフォルダのみ新しい順に返す。
show 時にバックグラウンドで先読み(10秒スロットル)。フォルダを開くとき
`_explorer_windows()`(IShellWindows)で既存エクスプローラのパスを照合し、
一致すれば前面化、なければ startfile。vtable 実測値: IShellWindows の
get_Count=7 / Item=8、IWebBrowser2 の get_LocationURL=30 / get_HWND=37。
ロック(📌)は `recent_pinned`(パス配列=表示順)に保存され先頭に固定表示。
ドラッグ(`_press`/`_drag_motion`/`_drag_drop`)でロック位置を並び替え、
未ロック項目のドロップはその位置にロック。ロジックは scratchpad の
test_pin_logic.py 方式(CONFIG_PATH を差し替えてヘッドレス実体化)で検証可。

### 実行中判定(3系統)

1. ProcessManager 追跡(自分で起動した Popen / adopt した pid)
2. adopt_running: 外部起動の取り込み。.bat=cmd.exe の CommandLine 照合、.exe=ExecutablePath 照合(CIM 1クエリ)。多プロセス app(Electron等)は ParentProcessId でルートを選んで adopt(子レンダラを掴むと停止が部分的にしか効かない)。どちらも外れたら**稼働判定ポートの所有 pid を adopt**(`_tcp_listeners` = GetExtendedTcpTable、サブプロセス不要。MCP が bat を経由せず起動した ComfyUI 対策)。起動時+トレイ再スキャン+ポップアップ表示時(60秒スロットル)。**「startで実体を起動して即exitするラッパーbat」は追跡から見えない** → 項目の path を実体exeに変えて args/cwd で補う(Octopus の例)
3. ポート稼働(上記ウォッチャー)。`_is_active` = 1 or 3

stop() は tracked ツリー kill 後もポートが生きていればポート所有者ツリーを
kill(最終フォールバック)。restart() はポート解放を待ってから launch
(即 launch すると新インスタンスが bind で死ぬ)。

### 複数インスタンス検出(scan_exe/scan_re)

app 項目に `scan_exe`(既定 python.exe)+`scan_re`(CommandLine 照合)を
書くと、adopt_running が本来ポート以外で LISTEN する同種プロセスを
`pm.instances`(name→[{port,pid}])に集め、`_refresh_list` が親項目直下に
合成項目 `type:"_proc"`(名前 `ComfyUI :8189`)として展開する。Enter=
ブラウザで開く、右クリック→停止= pid ツリー kill。表示時に `_pid_alive`
で死骸をフィルタ。IPC `status` の `_instances` で確認可。ComfyUI 実設定:
`"scan_re": "(?i)comfyui[\\\\/]main\\.py"`(MCP 直起動は
`python.exe ComfyUI\main.py --port XXXX` 形式で bat を通らない)。

### wingroup(CLIセッション検出)

タイトルが `title_re` に一致するターミナルウィンドウ(CASCADIA_HOSTING_WINDOW_CLASS /
ConsoleWindowClass)を `_refresh_list` 内で毎回列挙して合成項目 `type:"_win"` に展開。
表示中は毎秒フル再生成(選択は名前で維持)。`_win_cache` により、一度検出した
ウィンドウはタイトルが一時的に非一致になっても生存していれば120秒リストに残す
(エージェント動作中のタイトル瞬断対策)。Claude Code のタイトルは
「✳(待機)/ U+2800台スピナー(動作中)+ セッション名」。スピナー先頭なら
`busy` フラグが立ち、ポップアップ表示中は `_anim_tick`(300ms)が Clawd
(公式ドット絵から抽出した 12x8 グリッド、`_CLAWD_BASE`)をホップさせる
(非表示中はラベル更新なし=負荷ゼロ)。スプライトは純 Tk PhotoImage
(透過=未描画ピクセル)で、参照を `self._mascot` に保持しないと消える。**昇格ターミナルは
タイトルに「管理者: 」が付く**ので判定前に剥がす(英語 Administrator も)。
注意: claude シムでタイトル上書きを無効化すると busy が取れなくなるので、
シムは初期タイトル設定のみ(CLAUDE_CODE_DISABLE_TERMINAL_TITLE は使わない)。

## ハマりどころ(全部実際に踏んだ)

- **overrideredirect Toplevel の初回 show は約1秒かかる**(フォント/絵文字フォールバック読込)。起動時に alpha=0 で一度マップ+リスト描画して温めておく
- **focus_force は ALT の keybd_event ナッジがないと前面化に失敗することがある**(フォアグラウンドロック)
- **フォーカスアウト自動クローズ**は Tk メニュー/ファイルダイアログ表示中に誤発火する → `_menu_open` フラグでガード
- **grid の列数を減らすと旧列の weight/uniform が残って左寄りになる** → 減らした分は weight=0, uniform="" でリセット
- **selfrestart は cmd /c start 中継だと引用符地獄で壊れる** → pythonw を直接 DETACHED_PROCESS で起動し、新プロセス側が `--wait-port` でポート解放を待つ。旧インスタンスのホットキー解放とも競合するので登録は 0.2s×15 リトライ
- **SetWindowsHookExW は 64bit で GetModuleHandleW の restype を HMODULE にしないと失敗**(戻り値がintに切り捨てられる)
- **「手前に表示」のタイトル部分一致は誤爆する**(画像ビューアのファイル名等)。URL系はブラウザのウィンドウクラス(MozillaWindowClass / Chrome_WidgetWin)限定、ツール系はプロセスツリー照合優先。**exe名判定はサンドボックス版Firefoxで壊れる**(イメージ名が mozGUID になる)のでクラス名で
- **EnumWindows は DWM クローク窓(UWPゴースト)も可視扱い** → DWMWA_CLOAKED(14) でフィルタ
- **Windows Terminal のタブ**: ウィンドウタイトル=アクティブタブのみ。背後のタブは検出不能(UIA を使わない限り)
- **タイトルを設定しないCLI(Codex等)** はラッパー bat に `title X - %CD%` を1行足せば検出可能に
- **自動再起動型スーパーバイザ**(例: AI-Toolkit の `concurrently --restart-tries -1`)は子を殺しても1秒で蘇生する。ポート所有プロセスから親(node/cmd)を遡って根元を taskkill /T /F(`stop_ai_toolkit.py` 参照)。`taskkill /IM node.exe` のような全殺しは絶対にしない
- **Win11 エクスプローラのタブは同一 hwnd を共有する**。IShellWindows は
  タブごとに1エントリ返すが hwnd は同じ → 前面化はウィンドウ単位まで
  (目的のタブが選択されるとは限らない)。UIA を使わない限り WT のタブと同じ制限
- **os.path.isdir は切断されたネットワークパスで数秒ブロックする** →
  Recent の解決(isdir 含む)は必ずワーカースレッドで。Tk スレッドでやると
  ポップアップが固まる
- **「背景だけ透過・文字は不透明」は実装を断念した(2026-07-10、再挑戦非推奨)**。
  Tk の -alpha は文字ごと薄くなるので不可。SetWindowCompositionAttribute の
  ACCENT_ENABLE_ACRYLICBLURBEHIND は build 26200 では GradientColor の
  暗色ティントが無視され「白っぽい磨りガラス」になるだけ(状態2/3/4とも)。
  カラーキー(-transparentcolor)は透明部分のクリックが背面に抜けるので不採用。
  ユーザー判断で機能ごと廃止済み。次に頼まれたら DirectComposition 等の
  全面書き直しが必要になる旨を先に伝えること
- **ホットキーが「効かない」デバッグ手順**: ①launchit.log の `hotkey fired (foreground=...)` を見る(届いているか)→ ②合成キー(keybd_event)で動くか → ③動くのに物理で動かないなら WH_KEYBOARD_LL の一時ロガーで LLKHF_INJECTED フラグ付きで記録 → **ユーザーが実際に押しているキーをまず疑う**(本件の実例: Shift+Space と言いながら Ctrl+Space を押していた)。UAC 無効環境(EnableLUA=0)では昇格説は成立しない
- **パススルーの打ち返しは「そのままキーを再合成」では届かない**(実例:
  AE の VideoCopilot FX Console、2026-07-22 に実測解決)。必要条件は3つ:
  ①スキャンコード必須(MapVirtualKeyW で付与。scan=0 は GetKeyNameTextW
  系の照合に不可視)②WM_HOTKEY に食われた物理 down はキー状態が押下中の
  まま残るので、先に up を注入してから down しないとオートリピート扱いで
  無視される ③down は 0.1s 保持してから up(GetKeyState ポーリング型は
  瞬間 down/up を観測できない)。修飾キーはユーザーが物理で押しっぱなし
  なので再合成不要(トリガーキーのみ注入)。FX Console のポップアップは
  AfterFX プロセスの `VCSDK_WINDOW_CLASS`/'Effects Popup' ウィンドウ
  (非表示で待機、フォーカス喪失で自動クローズ)なので可視化の有無で検証可

## 検証方法

GUI テストは scratchpad に使い捨てスクリプトを書く方式(リポジトリには含めない):

- ポップアップ表示確認: IPC で show → `EnumWindows` でタイトル "LaunchIt" + `IsWindowVisible`
- ホットキー実発火: `keybd_event` で合成入力(RegisterHotKey は合成でも発火する)
- ホットキー保持者の確認: 別プロセスから `RegisterHotKey(None, 99, mods, vk)` を試す(失敗=誰かが保持)
- プロセス制御: ダミー bat(`ping -n 60 >nul`)を launch → 子pid確認 → stop → ツリー全滅確認
- 稼働状況: IPC `status` が全項目の tracked/port と `_sessions`(検出中CLIセッション)を JSON で返す

注意: テスト実行はユーザーの実マシン上。フォーカスを奪う・カーソルを動かすテストは
最小限にし、動かした物は元に戻す。ユーザーの実操作と競合して偽陽性が出ることがある
(showテストのタイムアウトは大抵これ)。

## 中断時点の状態 (2026-07-14)

- ポート所有者 adopt / stop フォールバック / scan_re 複数インスタンス表示を
  追加(上記)。実例: ComfyUI MCP サーバーが 8188 を bat 非経由で起動 →
  3系統のどれにも掴めず停止・再起動が no-op になっていた
- ヘッドレス検証: scratchpad の test_port_adopt.py 方式(ダミー
  `python -m http.server` 2本で adopt/scan/stop フォールバックを実測)

## 以前の状態 (2026-07-09)

- v1 完成・全機能検証済み・GitHub に公開済み。既知バグなし
- 同日追加: launchit.ico(オレンジのロケット、生成スクリプトは scratchpad)、
  スタートアップ登録済み(LaunchIt.lnk → LaunchIt.vbs)、手動起動用 LaunchIt.bat、
  最近使ったフォルダビュー(Space トグル)
- Claude Code セッション検出は `%USERPROFILE%\.local\shims\claude.cmd`
  (title 固定 + CLAUDE_CODE_DISABLE_TERMINAL_TITLE=1)経由で確実化。
  shims は PATH 未登録(手打ち claude は従来の ✳/スピナー正規表現で検出)
- ローカルでは常駐インスタンスが稼働中(ユーザー設定は launchit.json)
- 未実装のアイデア: Windows Terminal 背後タブの UIA 列挙、ブラウザ拡張による
  タブ直接フォーカス、項目アイコン、ドラッグ&ドロップ追加
