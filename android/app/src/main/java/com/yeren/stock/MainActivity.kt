package com.yeren.stock

import android.app.Activity
import android.graphics.Color
import android.os.Bundle
import android.util.Log
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.webkit.WebChromeClient
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ProgressBar
import android.widget.ScrollView
import android.widget.TextView
import com.chaquo.python.PyObject
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import java.io.File
import kotlin.concurrent.thread

/**
 * 外壳 Activity：拉起内嵌 Python 服务，然后让 WebView 去访问
 * http://127.0.0.1:<port>。界面、逻辑全部由已有的看板页面提供，
 * 这里只负责「把服务跑起来 + 显示出来 + 出错时把原因摊开给人看」。
 */
class MainActivity : Activity() {

    private lateinit var root: FrameLayout
    private lateinit var web: WebView
    private lateinit var statusBox: LinearLayout
    private lateinit var statusText: TextView
    private lateinit var bar: ProgressBar
    private var port = 0

    companion object {
        private const val TAG = "YerenStock"
        private const val DEFAULT_PORT = 8777

        @Volatile
        private var bootedPort = 0
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        buildUi()
        bootPython()
    }

    // ────────────────────────────── 界面骨架

    private fun buildUi() {
        root = FrameLayout(this)

        web = WebView(this).apply {
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
            visibility = View.GONE
            settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true          // 看板用 localStorage 记当前页
                databaseEnabled = true
                useWideViewPort = true
                loadWithOverviewMode = true
                builtInZoomControls = false
                displayZoomControls = false
                cacheMode = WebSettings.LOAD_NO_CACHE
                mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            }
            webViewClient = WebViewClient()
            webChromeClient = WebChromeClient()
            setBackgroundColor(Color.parseColor("#f5f7fa"))
        }

        statusBox = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER
            setPadding(48, 48, 48, 48)
            setBackgroundColor(Color.parseColor("#f5f7fa"))
            layoutParams = FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT
            )
        }

        val title = TextView(this).apply {
            text = "野人体系 · 选股看板"
            textSize = 18f
            setTextColor(Color.parseColor("#1f2933"))
            gravity = Gravity.CENTER
        }
        statusText = TextView(this).apply {
            text = "正在启动内嵌引擎…"
            textSize = 13f
            setTextColor(Color.parseColor("#52606d"))
            gravity = Gravity.CENTER
            setPadding(0, 24, 0, 24)
        }
        bar = ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal).apply {
            isIndeterminate = true
            layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 8
            )
        }

        statusBox.addView(title)
        statusBox.addView(statusText)
        statusBox.addView(bar)

        root.addView(web)
        root.addView(statusBox)
        setContentView(root)
    }

    // ────────────────────────────── 启动 Python

    /**
     * 把 assets/ 整个解到私有目录（web/ 页面 + defaults/ 默认策略）。
     * Chaquopy 只打包 .py，这两个目录进不了它的源码包，所以得由这里递过去。
     * 每次启动都覆盖写：升级 App 后页面跟着换新，不会读到旧缓存。
     */
    /**
     * 关键资源清单。显式列出，不走目录枚举 —— AssetManager 的目录语义在
     * 各家 ROM 上不完全一致，靠枚举容易踩空。目录树扫描只作为兜底（补以后
     * 新增的资源），主路径走这里。
     */
    private val knownAssets = listOf(
        "web/index.html",
        "defaults/strategy.json"
    )

    private fun extractAssets(): String {
        val dst = File(filesDir, "assets")
        dst.mkdirs()

        // 1) 主路径：按清单逐个解出来
        val missing = mutableListOf<String>()
        for (rel in knownAssets) {
            val out = File(dst, rel)
            out.parentFile?.mkdirs()
            try {
                assets.open(rel).use { input ->
                    out.outputStream().use { output -> input.copyTo(output) }
                }
            } catch (t: Throwable) {
                missing.add(rel)
                Log.w(TAG, "asset 缺失: $rel", t)
            }
        }

        // 2) 兜底：扫目录树，补上清单外的资源（失败不影响主路径）
        runCatching { copyAssetDir("", dst) }
            .onFailure { Log.w(TAG, "目录树扫描失败（已忽略）", it) }

        val index = File(dst, "web/index.html")
        if (!index.exists() || index.length() <= 0L) {
            throw IllegalStateException(
                "解包后没有可用的看板页面。\n" +
                    "缺失资源: ${missing.joinToString().ifEmpty { "(清单都读到了)" }}\n" +
                    "assets 顶层: " + assets.list("").orEmpty().joinToString() + "\n" +
                    "解包目录: " + dst.absolutePath + "\n" +
                    "web 目录: " + (File(dst, "web").list().orEmpty().joinToString())
            )
        }
        return dst.absolutePath
    }

    private fun copyAssetDir(assetPath: String, dst: File) {
        for (name in assets.list(assetPath).orEmpty()) {
            // 注意：assetPath 为空时不能拼成 "/web"。开头的斜杠会让
            // assets.list 返回空，于是把目录误判成文件、open() 直接抛异常
            // （v1.12.0 首版就栽在这，表现为页面 404）。
            val childAsset = if (assetPath.isEmpty()) name else "$assetPath/$name"
            val sub = assets.list(childAsset).orEmpty()
            if (sub.isEmpty()) {
                // 叶子节点：当文件处理
                val out = File(dst, name)
                out.parentFile?.mkdirs()
                assets.open(childAsset).use { input ->
                    out.outputStream().use { output -> input.copyTo(output) }
                }
            } else {
                val dir = File(dst, name)
                dir.mkdirs()
                copyAssetDir(childAsset, dir)
            }
        }
    }

    private fun bootPython() {
        thread(name = "py-boot") {
            try {
                if (!Python.isStarted()) {
                    Python.start(AndroidPlatform(applicationContext))
                }

                // 页面资源解不出来就直接报错 —— 否则会一路走到 Python 那边
                // 服务起得来但页面 404，看起来像"能连上却没内容"，很难查
                val assetsDir = try {
                    extractAssets()
                } catch (t: Throwable) {
                    Log.e(TAG, "extract assets failed", t)
                    showError("看板页面解包失败", t.message ?: t.stackTraceToString())
                    return@thread
                }

                if (bootedPort == 0) {
                    val boot: PyObject = Python.getInstance().getModule("android_boot")
                    val p = boot.callAttr(
                        "start",
                        filesDir.absolutePath,
                        cacheDir.absolutePath,
                        DEFAULT_PORT,
                        assetsDir
                    ).toInt()

                    if (p == 0) {
                        val err = runCatching {
                            boot.callAttr("last_error").toString()
                        }.getOrDefault("(无错误信息)")
                        showError("内嵌引擎启动失败", err)
                        return@thread
                    }
                    bootedPort = p
                    Log.i(TAG, "python server on port $p")
                    runCatching {
                        Log.i(TAG, Python.getInstance()
                            .getModule("android_boot").callAttr("diag").toString())
                    }
                }
                port = bootedPort
                runOnUiThread { loadBoard() }
            } catch (t: Throwable) {
                Log.e(TAG, "boot failed", t)
                showError("启动异常", t.stackTraceToString())
            }
        }
    }

    private fun loadBoard() {
        statusText.text = "正在连接 http://127.0.0.1:$port …"
        // 服务刚起，偶尔第一下连不上，失败就自动重试一次
        web.webViewClient = object : WebViewClient() {
            override fun onPageFinished(view: WebView?, url: String?) {
                statusBox.visibility = View.GONE
                web.visibility = View.VISIBLE
            }

            override fun onReceivedError(
                view: WebView?, request: android.webkit.WebResourceRequest?,
                error: android.webkit.WebResourceError?
            ) {
                if (request?.isForMainFrame == true && error != null) {
                    view?.postDelayed({
                        if (port != 0) view.loadUrl("http://127.0.0.1:$port/")
                    }, 1200)
                }
            }
        }
        web.loadUrl("http://127.0.0.1:$port/")
    }

    private fun showError(title: String, detail: String) {
        runOnUiThread {
            web.visibility = View.GONE
            statusBox.removeAllViews()

            val t = TextView(this).apply {
                text = title
                textSize = 16f
                setTextColor(Color.parseColor("#c81e1e"))
                gravity = Gravity.CENTER
            }
            val d = TextView(this).apply {
                text = detail
                textSize = 11f
                setTextColor(Color.parseColor("#52606d"))
                setPadding(0, 20, 0, 0)
            }
            val holder = ScrollView(this)
            holder.addView(d)
            holder.layoutParams = LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, 0, 1f
            )

            statusBox.addView(t)
            statusBox.addView(holder)
            statusBox.visibility = View.VISIBLE
        }
    }

    // ────────────────────────────── 生命周期 / 返回键

    override fun onBackPressed() {
        if (::web.isInitialized && web.canGoBack()) {
            web.goBack()
        } else {
            @Suppress("DEPRECATION")
            super.onBackPressed()
        }
    }
}
