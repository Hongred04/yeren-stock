# 野人体系选股 · 安卓版

把桌面上的整套 Python 选股引擎 + 看板页面打包成一个独立 APK。
**手机自己抓数据、自己算**，不依赖电脑开机，出门用流量也能看。

- 包名：`com.yeren.stock`　应用名：野人体系
- 版本：跟随主工程 `core/version.py`
- 支持：Android 7.0 (API 24) 及以上，**仅 arm64-v8a**（2017 年后的真机都是）
- 体积：约 13 MB

---

## 它是怎么跑起来的

```
APK
├── lib/arm64-v8a/libpython3.13.so     ← Chaquopy 内置的 CPython
├── assets/chaquopy/app.imy            ← 主工程的 core/ + server.py（预编译成 .pyc）
├── assets/web/mobile.html             ← 手机端精简看板（App 加载这个）
├── assets/web/index.html              ← 桌面版完整页面（App 内不加载，保留备用）
├── assets/defaults/strategy.json      ← 桌面调好的默认策略
└── MainActivity (Kotlin)
        │
        ├─ 1. 把 assets 解到 App 私有目录（Chaquopy 不打包 .py 以外的文件）
        ├─ 2. 启动 Python，调 android_boot.start(...)
        │        └─ 在后台线程里跑 ThreadingHTTPServer，监听 127.0.0.1:8777
        └─ 3. WebView 打开 http://127.0.0.1:8777/mobile.html
```

**手机端 UI 是独立设计的**（v1.21 起）：`web/mobile.html` —— 只保留最重要的
六块（今日结论 / 情绪三格 / 主线题材 / 候选股 / 持仓 / 明日判断），
候选与持仓点开是底部抽屉详情；支持下拉刷新、深色模式跟随系统。
桌面版 `index.html` 一个字节都没动。

关键点：**桌面工程一个文件都没改逻辑**。所有安卓特有的差异收在
`core/paths.py` 一个文件里 —— 桌面下数据写 `data/`，安卓下写 App 私有目录。

**外部链接走系统浏览器**（v1.23.0）：`MainActivity` 的 `shouldOverrideUrlLoading`
把非 `127.0.0.1` 的地址一律交给系统浏览器，看板本身留在 WebView 不动。原因：东财
被限速时要在**网页**上做滑块验证，WebView 里做不了；而 `window.open()` 在未开
多窗口支持时返回 `null`、静默失效，所以页面侧用 `window.open → location.href`
兜底，再由这里拦下来转交系统浏览器。

```
android/app/src/android_glue/android_boot.py   ← 安卓启动引导（注入目录、起服务）
android/app/src/main/java/.../MainActivity.kt  ← WebView 外壳（加载 /mobile.html）
android/app/build.gradle                       ← 同步任务 + Chaquopy 配置
```

---

## 重新打包

改完主工程代码后，在 `android/` 目录双击 **`build_apk.bat`** 即可。
脚本会先把主工程的 `core/`、`server.py`、`web/`、`data/strategy.json`
同步进来，再调用 Gradle 构建。

产物：`app/build/outputs/apk/debug/app-debug.apk`

命令行等价操作：

```bash
gradle :app:assembleDebug
```

### 工具链

构建需要 JDK 17 + Android SDK + Gradle，本机装在 `C:\Users\macro\android-toolchain`：

| 组件 | 版本 | 位置 |
|---|---|---|
| JDK | Microsoft OpenJDK 17.0.13 | `jdk17/` |
| Android SDK | platform-34 / build-tools 34.0.0 | `sdk/` |
| Gradle | 8.7 | `gradle-8.7/` |
| 构建期 Python | 3.13（PATH 上的 python） | — |

换机器时用环境变量 `ANDROID_TOOLCHAIN` / `JAVA_HOME` / `ANDROID_HOME` 覆盖即可。

> ⚠️ 本机 **github.com 不可达**，而 Gradle 官方发行包恰好托管在 GitHub，
> 所以 Gradle 是从腾讯镜像下的；依赖仓库在 `settings.gradle` 里配了
> 阿里云镜像 + 官方源兜底。换网络环境时注意这一点。

---

## 安装

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

或者把 apk 拷进手机，用文件管理器点开安装（需允许「安装未知来源应用」）。

这是 **debug 签名** 的包 —— 自用没问题，但不能上应用商店。
如果以后要发布，得换成自己的 release keystore。

---

## 手机上怎么用

打开 App 后：

1. 首次启动会看到「正在启动内嵌引擎…」，约 1～3 秒进入看板。
2. 界面和电脑上**完全一样**：五个一级菜单（今日总览 / 市场情绪 / 资金动向 /
   选股决策 / 我的持仓），点底部或顶部标签切换。
3. 选股要跑一次全市场扫描（约 6000 只），**点首页的「重新扫描」**才触发，
   耗时 15～30 秒，取决于手机网络。
4. 持仓 / 自选记录存在手机本地（App 私有目录），和电脑上的 `data/portfolio.json`
   **不互通** —— 两边各自独立。

### 手机上的注意事项

- **策略配置**：首启会把随包的默认策略（桌面那份）放进可写区，之后在
  「调策略」面板里的改动只影响手机，升级 App 不会覆盖。
- **自选股要重新加**：因为数据不互通。
- **屏幕旋转**：已在 manifest 里声明 `configChanges`，旋转不会重启服务。
- **后台**：切到后台服务仍在跑，但被系统回收后会随 App 重启一起恢复。

---

## 排查

App 起不来时会**直接把错误摊在屏幕上**（不是白屏），包括 Python 异常栈。
也可以用 adb 看日志：

```bash
adb logcat -s YerenStock
```

启动时会打印一行 `diag()` 诊断，形如：

```
root=/data/user/0/com.yeren.stock/files/chaquopy/...
data=/data/user/0/com.yeren.stock/files/data
web=.../files/assets/web  web_exists=True  writable=True
```

| 现象 | 原因 | 处理 |
|---|---|---|
| 一直卡在「正在启动内嵌引擎…」 | Python 启动异常 | 看屏幕上的错误栈或 logcat |
| 显示 **Error response / 404 / Nothing matches the given URI** | 页面资源没解到私有目录 | v1.12.1 已修（见下）；确认 `web_exists=True` |
| 显示「看板页面解包失败」 | assets 里确实没有页面 | 重新打包，确认 `assets/web/index.html` 在 APK 里 |
| 取不到行情 | 手机无网 / 抓数接口被限流 | 见主工程 README「数据源治理」 |
| 扫描很慢 | 首次扫描要全市场枚举 | 第二次起走当日有效代码名单，会快很多 |

### 那个 404 是怎么回事（v1.12.0 → v1.12.1）

装上打开后看到 Python 的 `404 Nothing matches the given URI`，看着像"服务连上了却没内容"。

根因是 `MainActivity.extractAssets()` 里递归拼子路径时，`"$assetPath/$name"`
在 `assetPath` 为空的情况下拼出了 `/web`（**前导斜杠**）。
Android 的 `AssetManager` 对带前导斜杠的路径一律返回空列表，于是 `web` 被当成
**文件**处理、`assets.open("/web")` 抛异常，整个函数被 `catch` 吞掉静默返回 `null`；
接着 `STOCK_WEB_DIR` 根本没被设置，Python 退回只读的源码目录找 `web/` → 404。

现在：空 path 不再加斜杠；解包改为**按显式清单**（`web/index.html`、
`web/mobile.html`、`defaults/strategy.json`）为主、目录树扫描为兜底，不再依赖
各家 ROM 的 `AssetManager` 目录语义；解包失败会**把缺失清单和目录内容直接摆到
屏幕上**，而不是落到一个看不出所以然的 404 页面。

> ⚠️ 清单里**必须**有 `web/mobile.html`（App 加载的就是它）。原来只列了
> `index.html`，mobile.html 全靠目录扫描兜底 —— 扫描一旦失配就是"能连上却没内容"。
> 改前端入口时同步 `MainActivity.knownAssets` 与 `test_asset_walk.py` 的 `KNOWN`。

---

## 自检脚本（打包前跑）

在 `android/` 目录下：

```bash
# 验证 assets 解包逻辑（含 v1.12.0 那个 404 的复现）
python test_asset_walk.py

# 验证 APK 里的 Python 能启动、能服务页面、数据落在私有目录
python verify_packaged_python.py

# 端到端：用打包的那份代码真跑一次全市场选股
python verify_engine_e2e.py
```

这三个脚本不依赖手机：`test_asset_walk.py` 用模拟 `AssetManager` 语义的假对象
逐行跑解包逻辑；后两个直接从 APK 里解出资源、按 `MainActivity` 的路径启动服务并 curl。

> ⚠️ **教训**：验证必须打在被测的那段逻辑上。v1.12.0 的 404 之所以漏掉，
> 就是因为当时的验证脚本**用 Python 重写了一遍解包流程**，
> Kotlin 里真正的路径拼接一次都没跑到 —— 重写一份等于没测。
