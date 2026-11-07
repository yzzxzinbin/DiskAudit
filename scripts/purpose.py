# -*- coding: utf-8 -*-
"""目录用途知识库：内置通用基础库 + 读取 / 对账。

两个职责：
    ① 提供一份**不绑定任何机器**的基础库（EXACT / NAMES / PATHS），
       新环境第一次启动时写进 content/purpose.json，让报告立刻能出。
    ② 按页面同款顺序（精确 → 名字 → 路径 → 祖先继承）判断每条路径的兜底档次，
       供 ticket.py 挑出「该补哪些目录」。

不提供命令行入口 —— 调用方是 ticket.py 与 app.py。
"""
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

import pathvars

# ---------------------------------------------------------------------------
# 通用基础库：只放「任何 Windows 机器都成立」的内容
#   · 不出现账户名（模板里用 %HOME% 代替）
#   · 不出现具体体积（那种数字换台机器就不对了）
#   · 系统目录用 %SYSDRIVE%，被扫描的盘用 %DRIVE%，两者不能混
#   · 评级 g：ok 可清理 / warn 谨慎 / no 别动 / info 了解即可
#
# 模板里的占位符只是「跨机器复用」的写法，**落盘时必须展开**：
# build() 会按当前机器换成人话路径，并丢掉不属于本盘的条目。
# 数据文件里不该出现 %HOME% 这类记号 —— 那是解析期才需要的东西。
# ---------------------------------------------------------------------------

NOTE = ("目录用途知识库。取值顺序：精确路径 → 名字规则 → 路径规则 → 最近祖先继承。"
        "路径一律是真实绝对路径，各盘独立一份；缺口由 ticket.py 汇总成 ticket，交给 AI 补。")

TAGS = {
    "ok": "可以清理",
    "warn": "谨慎 / 部分可回收",
    "no": "别动",
    "info": "了解即可",
}

EXACT = [
    # ---- 被扫描盘根 / 系统盘 ----
    ("%DRIVE%:\\", "被扫描盘的根目录。系统盘上通常有 Windows、Program Files、ProgramData、Users 四个大目录，"
                  "再加上 pagefile.sys / hiberfil.sys 这类系统文件。", "info"),

    # ---- Windows ----
    ("%SYSDRIVE%:\\Windows", "Windows 系统目录。WinSxS（组件存储）通常是其中最大的一块，"
                         "它与 System32 之间存在硬链接共享，两者相加会重复计数。", "no"),
    ("%SYSDRIVE%:\\Windows\\WinSxS", "组件存储，存放所有系统组件的多个版本。账面体积因硬链接而严重虚高，"
                                 "不要手动删；要回收用 DISM /StartComponentCleanup。", "no"),
    ("%SYSDRIVE%:\\Windows\\System32", "系统核心程序与驱动，不要动。", "no"),
    ("%SYSDRIVE%:\\Windows\\SysWOW64", "32 位兼容层，不要动。", "no"),
    ("%SYSDRIVE%:\\Windows\\Installer", "MSI 安装包缓存。卸载、修复、更新时都会回查这里，删了对应操作会失败。", "no"),
    ("%SYSDRIVE%:\\Windows\\SoftwareDistribution", "Windows 更新的下载与分发目录。停掉 wuauserv 后可清，"
                                               "系统会按需重新下载。", "warn"),
    ("%SYSDRIVE%:\\Windows\\SoftwareDistribution\\Download", "已下载的更新包。装完后即无用，"
                                                         "停止更新服务后可以整个清空。", "ok"),
    ("%SYSDRIVE%:\\Windows\\Temp", "系统临时目录，可以清空（占用中的文件跳过即可）。", "ok"),
    ("%SYSDRIVE%:\\Windows\\Logs", "系统日志（CBS、DISM、更新日志等）。纯文本记录，可以清。", "ok"),
    ("%SYSDRIVE%:\\Windows\\Prefetch", "预读取缓存，用于加快程序启动。删掉会重建，只影响首次启动速度。", "ok"),
    ("%SYSDRIVE%:\\Windows\\assembly", ".NET 全局程序集缓存（GAC），程序运行期加载，不要动。", "no"),
    ("%SYSDRIVE%:\\Windows\\servicing", "组件服务数据，记录系统组件的安装状态，不要动。", "no"),
    ("%SYSDRIVE%:\\Windows\\Microsoft.NET", ".NET Framework 运行库，不要动。", "no"),
    ("%SYSDRIVE%:\\Windows\\Fonts", "系统字体，删了界面会缺字。", "no"),
    ("%SYSDRIVE%:\\Windows\\Minidump", "蓝屏小转储文件。只用于事后分析，可以删。", "ok"),
    ("%SYSDRIVE%:\\Windows\\LiveKernelReports", "内核实时转储报告。仅用于诊断，可以删。", "ok"),
    ("%SYSDRIVE%:\\Windows.old", "系统升级前的旧系统备份。升级确认没问题后可以删（或用磁盘清理删）。", "warn"),

    # ---- Program Files ----
    ("%SYSDRIVE%:\\Program Files", "64 位程序的默认安装位置。", "info"),
    ("%SYSDRIVE%:\\Program Files\\Common Files", "64 位程序共享组件（各家运行库、许可服务等）。", "no"),
    ("%SYSDRIVE%:\\Program Files\\WindowsApps", "Microsoft Store 应用包目录。整包由系统与 TrustedInstaller 管理，"
                                            "只能在「设置 → 应用」里卸载。", "no"),
    ("%SYSDRIVE%:\\Program Files (x86)", "32 位程序的默认安装位置，也常被开发工具用作安装目录。", "info"),
    ("%SYSDRIVE%:\\Program Files (x86)\\Common Files", "32 位程序共享组件。", "no"),
    ("%SYSDRIVE%:\\Program Files (x86)\\InstallShield Installation Information",
     "InstallShield 打包程序的卸载信息（隐藏目录）。删掉后对应程序无法正常卸载或修复。", "no"),

    # ---- ProgramData ----
    ("%SYSDRIVE%:\\ProgramData", "所有用户共享的程序数据目录。多数软件把配置、缓存、许可信息放这里。", "info"),
    ("%SYSDRIVE%:\\ProgramData\\Package Cache", "安装器的产品包缓存（目录名是 {产品GUID}v{版本}）。"
                                            "用于修复与修改安装；确认软件稳定后可清，之后修复安装需联网重下。", "warn"),
    ("%SYSDRIVE%:\\ProgramData\\Microsoft", "微软组件的共享数据（Defender 定义、Windows 更新等）。", "no"),

    # ---- 用户目录骨架 ----
    ("%SYSDRIVE%:\\Users", "所有本机账户的家目录。", "info"),
    ("%HOME%", "当前账户的家目录。体积主要由 AppData（软件配置与缓存）、Downloads 和各类工具目录构成。", "info"),
    ("%HOME%\\AppData", "本账户的程序数据。分 Local（本机缓存与数据）、Roaming（跟随账户漫游的配置）、"
                        "LocalLow（低权限进程数据）三块。", "info"),
    ("%HOME%\\AppData\\Local", "本机范围的程序数据与缓存，通常是家目录里最大的一块。", "info"),
    ("%HOME%\\AppData\\Local\\Temp", "当前用户的临时目录。安装器解包、程序运行时中间文件都堆在这里，可以清空。", "ok"),
    ("%HOME%\\AppData\\Local\\Packages", "UWP / 商店应用的本地数据。逐个目录看没有意义 —— "
                                         "卸载对应应用时这份数据才会释放。", "warn"),
    ("%HOME%\\AppData\\Local\\Microsoft", "微软桌面应用的缓存与数据（Edge、Office、Teams 等）。", "info"),
    ("%HOME%\\AppData\\Roaming", "跟随账户漫游的程序配置。多数是配置和小数据，删了会丢设置。", "info"),
    ("%HOME%\\AppData\\Roaming\\Microsoft", "微软应用的配置（Office 模板、输入法词库等）。", "no"),
    ("%HOME%\\AppData\\LocalLow", "低权限进程（浏览器插件、Unity 游戏等）的数据目录。", "info"),
    ("%HOME%\\Downloads", "下载目录。往往是家目录里增长最快的一块，装完的安装包可以清。", "warn"),
    ("%HOME%\\Documents", "文档目录，属于个人内容。", "no"),
    ("%HOME%\\Desktop", "桌面，属于个人内容。", "no"),
    ("%HOME%\\Pictures", "图片目录，属于个人内容。", "no"),
    ("%HOME%\\Videos", "视频目录，属于个人内容。", "no"),
    ("%HOME%\\Music", "音乐目录，属于个人内容。", "no"),
    ("%HOME%\\OneDrive", "OneDrive 同步目录。删除本地副本会连带影响云端，先确认同步状态。", "no"),
    ("%HOME%\\.cache", "各类工具共用的缓存目录（pip、uv、huggingface 等）。删了会重新下载。", "ok"),
    ("%HOME%\\.config", "命令行工具的配置目录。删了会丢设置。", "no"),
    ("%HOME%\\.npm", "npm 的包缓存。删了下次安装会重新下载。", "ok"),
    ("%HOME%\\.nuget", "NuGet 包缓存。删了下次还原会重新下载。", "ok"),
    ("%HOME%\\.gradle", "Gradle 的依赖与构建缓存，通常体积很大。删了下次构建会重新下载。", "ok"),
    ("%HOME%\\.m2", "Maven 本地仓库。删了下次构建会重新下载。", "ok"),
    ("%HOME%\\.cargo", "Rust 的包缓存与工具链。删了下次构建会重新下载。", "ok"),
    ("%HOME%\\.rustup", "Rust 工具链本体。删了需要重新安装。", "warn"),
    ("%HOME%\\go", "Go 的模块缓存与编译产物。删了下次构建会重新下载。", "ok"),
    ("%HOME%\\scoop", "Scoop 包管理器的安装目录（含已装软件）。", "no"),
    ("%HOME%\\.vscode", "VS Code 的扩展与缓存。extensions 是已装插件，删了要重装。", "warn"),

    # ---- 系统文件与特殊目录 ----
    ("%DRIVE%:\\hiberfil.sys", "休眠文件，大小约为物理内存的 40%。不用休眠可以 powercfg /h off 关掉，"
                              "能直接回收这块空间。", "warn"),
    ("%DRIVE%:\\pagefile.sys", "虚拟内存分页文件。不建议删除，可以改小或移到别的盘。", "no"),
    ("%DRIVE%:\\swapfile.sys", "UWP 应用用的交换文件，通常很小。", "no"),
    ("%DRIVE%:\\$RECYCLE.BIN", "回收站。清空即可释放，普通权限扫描不到内部明细。", "ok"),
    ("%DRIVE%:\\System Volume Information", "系统还原点与卷影副本。要在「系统保护」里调整配额或删除还原点，"
                                           "不能直接删目录。", "no"),
    ("%DRIVE%:\\$WinREAgent", "Windows 升级过程中留下的临时目录。升级成功后即为垃圾，重启后可删。", "ok"),
    ("%DRIVE%:\\$Windows.~BT", "Windows 升级的安装暂存目录。升级完成后可以删（或用磁盘清理）。", "ok"),
    ("%DRIVE%:\\$Windows.~WS", "Windows 升级的下载暂存目录。升级完成后可以删。", "ok"),
    ("%DRIVE%:\\Recovery", "恢复分区相关文件（WinRE 镜像）。不要删，删了系统无法进入恢复环境。", "no"),
    ("%DRIVE%:\\PerfLogs", "Windows 性能日志目录，默认为空，可以忽略。", "info"),
]

NAMES = [
    # ---- 缓存 / 日志 / 临时 ----
    (r"^(Cache|Caches|cache|cache2?|CacheStorage|Code Cache|GPUCache|GPUCacheData|CachedData|ShaderCache)$",
     "运行时缓存目录。删掉后应用会按需重建，首次启动会稍慢。", "ok"),
    (r"^(logs?|Logs?|traces?)$", "日志目录，纯文本记录，删除不影响功能。", "ok"),
    (r"^(tmp|Temp|temp)$", "临时目录，可以清空。", "ok"),
    (r"^temp[a-z0-9_-]*$", "安装器 / 程序的临时解包目录，安装结束后即无用。", "ok"),
    (r"^(Crashpad|CrashReports|crashpad|dumps?|minidumps?)$", "崩溃转储目录，仅用于上报问题，可以清。", "ok"),
    (r"^CrashDumps$", "崩溃转储文件目录，可以清。", "ok"),
    (r"^Service Worker$", "PWA / 网页应用的离线缓存，可以删。", "ok"),
    (r"^(BlobStorage|IndexedDB|Local Storage|Session Storage)$",
     "网页应用的本地存储，可能含登录态。删了相当于退出登录。", "warn"),
    (r"^[0-9a-f]{16}$", "以哈希命名的缓存子目录（着色器 / 编译缓存），可以删。", "ok"),
    (r"^[0-9a-f]{8,20}$", "以哈希命名的目录（缓存 / 检查点 / 运行时版本）。这类目录常会留下多份历史副本。", "warn"),

    # ---- 更新残留 ----
    (r"^@.*-updater$", "桌面应用的自动更新残留。新版本装好后，旧安装包与 pending 目录不会自动清理，"
                       "整体删除是安全的。", "ok"),
    (r"^[a-z0-9._-]+-updater$", "桌面应用的自动更新残留。新版本装好后就没用了，可以整体删除。", "ok"),
    (r"^installer\.exe$", "更新包本体。对应的新版本已经安装完成，这个文件只是残留。", "ok"),
    (r"^pending$", "待应用的更新文件（下载完成但尚未落地），可随 updater 目录一起删。", "ok"),
    (r"^(Product Cache|InstallerCache|Installers)$",
     "安装器的产品包 / 补丁缓存，装完就没用了，可以整体删除（下次修复安装需联网重下）。", "ok"),

    # ---- 安装包 / 压缩包 / 转储 ----
    (r"\.(msi|exe)$", "安装包 / 可执行文件。确认已安装或不再需要后可以删。", "info"),
    (r"^[0-9a-f]{5,8}\.msi$", "Windows\\Installer 里的补丁包缓存，命名就是随机的十六进制。程序卸载 / "
                              "修复时会回查，不要手删。", "no"),
    (r"\.(zip|7z|rar|tar|gz)$", "压缩包。确认内容已解压或不再需要后可以删。", "info"),
    (r"\.(dmp|mdmp)$", "进程崩溃转储文件，只用于事后分析，可以删。", "ok"),
    (r"\.(log|txt)$", "日志 / 文本记录文件。", "ok"),
    (r"\.(tmp|bak|old|swp)$", "临时或备份文件，可以删。", "ok"),
    (r"\.(evtx)$", "Windows 事件日志文件。可以在「事件查看器」里清空，或用磁盘清理回收。", "ok"),

    # ---- 版本号 / 日期 / 会话 ----
    (r"^\d+\.\d+([.-]\d+)*$", "版本号目录。这类目录常会保留多个历史版本，只留最新即可回收一部分空间。", "warn"),
    (r"^v\d+$", "工具的版本化数据 / 组件目录。升级后会新建一个，旧的不清就会一直留着。", "warn"),
    (r"^\d{4}-\d{2}-\d{2}$", "按日期归档的目录。日志类可以清，数据类请先确认。", "info"),
    (r"^\d{8}$", "按日期（YYYYMMDD）归档的目录，通常是当天的日志或缓存。", "info"),
    (r"^\d{3,6}$", "以数字命名的子目录（进程 ID 或数据分区号）。通常是每次运行生成的独立记录，旧的可以清。", "warn"),
    (r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
     "以 UUID 命名的会话 / 检查点目录。每个对应一次独立的会话或一次改动快照。", "warn"),
    (r"^\{[0-9A-Fa-f-]{36}\}$", "以 GUID 命名的子目录，通常是某个产品、组件或租户的数据分区。属程序数据。", "no"),
    (r"^\{[0-9A-Fa-f-]+\}v", "安装器 / 补丁的缓存条目（GUID + 版本号），用于修复与卸载。"
                             "建议用官方安装器或磁盘清理处理。", "no"),

    # ---- 程序本体：不要动 ----
    (r"^(bin|lib|Lib|Include|include)$", "该软件的可执行文件 / 库文件 / 头文件目录，属于程序本体。", "no"),
    (r"^(res|resources|Resources|assets|Assets?|static|locales|Locales)$",
     "程序的界面资源、图标、语言包。属于程序本体，删了界面会缺字缺图。", "no"),
    (r"^(Modules|components|Bundles?|plugins?|plugins?)$", "应用的模块 / 插件目录，属于程序本体。", "no"),
    (r"^(Data|data)$", "数据库 / 应用的实际数据目录 —— 名称虽短，删了数据就没了。", "no"),
    (r"^App$", "应用的界面层目录（常见于 Electron / .NET 应用），属程序本体。", "no"),
    (r"^AOT$", "提前编译（Ahead-Of-Time）后的本机代码，用于加快启动。删了会回退到即时编译。", "no"),
    (r"\.(dll|sys|ocx)$", "动态链接库 / 驱动模块，程序运行期加载，不要单独删。", "no"),
    (r"\.(cat)$", "数字签名目录文件，用于校验程序或驱动的签名，不要删。", "no"),
    (r"\.(xml|json|ya?ml|ini|cfg|conf|toml|plist)$", "配置 / 清单文件，记录程序的状态与设置，不要单独删。", "no"),
    (r"\.(sqlite|sqlite3|db)$", "SQLite 数据库文件，保存着应用的会话或业务数据，删除即丢失记录。", "no"),
    (r"\.asar$", "Electron 应用的打包资源（源码与静态文件打成一个包）。属于程序本体。", "info"),

    # ---- 运行时 / 工具链 ----
    (r"^(runtime|jre|jdk.*|jre[\d.]+)$", "自带 / 下载的运行时环境。删了宿主程序无法启动，需要重装。", "no"),
    (r"^jmods$", "JDK 的模块文件（.jmod），只在 jlink 裁剪自定义运行时时才用到。", "warn"),
    (r"^(envs|versions)$", "多版本 / 多环境的并列目录。这类目录常会同时留着好几个历史版本。", "warn"),
    (r"^(msys|mingw\d*|msys64)$", "MSYS2 / MinGW 工具链环境（gnu make、gcc 等）。删了对应构建会失败。", "warn"),
    (r"^(builder|compilers?|toolchains?)$", "构建 / 编译工具链目录，删了下次构建会重新下载。", "warn"),
    (r"^node_modules$", "npm 依赖目录，删除后需要 npm install 重新安装。", "warn"),
    (r"^(repos|repository)$", "代码仓库 / 本地依赖仓库，属于个人内容。", "no"),
    (r"^backups?$", "备份目录。删除即失去这份还原点，先确认没有回滚需求。", "warn"),

    # ---- 商店应用 / 系统组件 ----
    (r"^(Microsoft|microsoft)\.", "Microsoft Store 应用的程序目录，只能在「设置 → 应用」里卸载。", "info"),
    (r"^MicrosoftWindows?\.", "系统内置 UWP 应用的程序目录。", "no"),
    (r"^_(x64|neutral|x86|arm64)__[a-z0-9]{13}$", "Microsoft Store 应用包目录（按架构与发布者哈希命名）。"
                                                  "只能在「设置 → 应用」里卸载。", "no"),
    (r"^(amd64|x86|wow64|msil|arm64)_", "WinSxS 组件存储里的单个组件目录，按 架构_组件名_公钥标记_版本 命名。", "no"),
    (r"^(CBS|InFlight)$", "组件安装服务的数据目录，记录组件安装状态。删了系统更新会出错。", "no"),
    (r"^(v4\.0\.30319|v2\.0\.50727)$", ".NET Framework 的运行库版本目录，不要动。", "no"),
    (r"^(drivers?)$", "驱动文件目录。系统或打印子系统的驱动集中存放处，不要手删。", "no"),

    # ---- 浏览器 / 开发工具 ----
    (r"^(chromium|chromium_headless_shell)(-\d+)?$|^chrome-\d+$|^browsers$",
     "自动化工具下载的浏览器内核。删了下次运行会自动重新下载。", "ok"),
    (r"^(IdeaIC|PyCharm|WebStorm|GoLand|CLion|Rider|AndroidStudio)\d{4}\.\d+$",
     "JetBrains IDE / Android Studio 的版本化配置目录。升级后会新建一个，旧的可清。", "warn"),
    (r"^\.vs$", "Visual Studio 的隐藏工作目录（浏览数据库、IntelliSense 缓存）。关闭 VS 后可删，会重建。", "ok"),
    (r"^sessions?$", "会话数据 / 会话日志目录。里面保存对话记录或运行过程，删除会丢失历史。", "warn"),
    (r"^marketplaces?$", "插件市场的索引与下载缓存，删了会重新拉取。", "ok"),
    (r"^(skills|plugins?)$", "技能 / 插件文件，属于程序功能的一部分。", "no"),
    (r"\.onnx(e)?$", "ONNX 神经网络模型文件。离线翻译、语音识别这类本地 AI 功能会用，删了功能失效。", "warn"),
]

# 注意：paths 的 src 是**正则**，不能写 %HOME% / %DRIVE% / %SYSDRIVE%。
# 占位符会被 pathvars 展开成 C:\Users\<user> 这样的单反斜杠路径，
# 而 \U 在正则里是非法转义（incomplete escape \U）。所以盘符与账户一律用正则通配：
#   [A-Z]:        任意盘符
#   [^\\]+        任意单层目录名（账户名、应用包名）
PATHS = [
    (r"^[A-Z]:\\Program Files\\WindowsApps\\[^\\]+$",
     "Microsoft Store 应用包目录。整包由系统与 TrustedInstaller 管理，只能在「设置 → 应用」里卸载。", "no"),
    (r"^[A-Z]:\\Program Files\\WindowsApps\\[^\\]+\\[^\\]+$",
     "Store 应用包内的程序文件（可执行文件、资源、模型）。逐个目录看没有意义。", "no"),
    (r"^[A-Z]:\\Windows\\WinSxS\\[^\\]+\\[^\\]+$",
     "组件存储里某个组件的内部文件。这一层与 System32、DriverStore 存在硬链接共享，"
     "账面体积会重复计算，不要按这里的数字判断能省多少。", "no"),
    (r"^[A-Z]:\\Windows\\Installer\\",
     "MSI 补丁包缓存文件。程序卸载、修复、更新时都会回查这里，删了对应操作会失败。", "no"),
    (r"^[A-Z]:\\ProgramData\\Package Cache\\",
     "安装器的产品包缓存（目录名是 {产品GUID}v{版本}）。用于修复与修改安装。", "warn"),
    (r"^[A-Z]:\\Users\\[^\\]+\\AppData\\Local\\Packages\\[^\\]+$",
     "UWP / 商店应用的本地数据。逐个目录看没有意义 —— 卸载对应应用时这份数据才会释放。", "warn"),
    (r"^[A-Z]:\\Users\\[^\\]+\\AppData\\Roaming\\JetBrains\\",
     "IntelliJ 系 IDE 的配置与索引目录，按「产品 + 大版本」分目录。升级后会新建一个。", "warn"),
]


def build(ctx=None):
    """建一份基础库。

    给了 ctx（pathvars.context 的返回值）就把模板里的占位符展开成真实路径，
    并丢掉**不属于本盘**的条目与规则 —— 每盘的库只装自己的路径。
    没给 ctx 时保留原样（只有在拿不到扫描数据时才走这条路，读的时候会再展开）。
    """
    letter = ((ctx or {}).get("%DRIVE%") or "").upper()
    exact = []
    for p, t, g in EXACT:
        q = pathvars.expand_str(p, ctx) if ctx else p
        if letter and not q.upper().startswith(letter + ":"):
            continue
        exact.append({"p": q, "t": t, "g": g, "he": True})

    paths = []
    for r, t, g in PATHS:
        # 规则里的 ^C: 是写死的盘符，装到别的盘上永远命不中，直接不带
        m = re.match(r"^\^([A-Za-z]):", r)
        if letter and m and m.group(1).upper() != letter:
            continue
        paths.append({"src": r, "t": t, "g": g, "he": True})

    return {
        "version": 1,
        "note": NOTE,
        "tags": TAGS,
        "exact": exact,
        "names": [{"re": r, "fl": "i", "t": t, "g": g, "he": True} for r, t, g in NAMES],
        "paths": paths,
    }

# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

BS = "\\"
MAX_ANCESTOR = 40          # 与前端 purposeOf() 一致

# JSON 里存的是 JS 的 flags 字符串（"i"、"gi" …），Python 的 re 要 int 位标志
_FLAG_MAP = {"i": re.I, "m": re.M, "s": re.S, "x": re.X, "u": 0, "g": 0}


def js_flags(fl):
    f = 0
    for ch in (fl or ""):
        f |= _FLAG_MAP.get(ch, 0)
    return f


def rd(p):
    if not os.path.exists(p):
        return None
    try:
        with io.open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        sys.stderr.write("[warn] 读不了 %s：%s\n" % (p, e))
        return None


def wr(p, obj):
    """原子写：先写 .new 再 replace，中途失败不会留下半个文件。"""
    d = os.path.dirname(p)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = p + ".new"
    with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, p)


def ensure(path, tree=None):
    """知识库不存在就按这台机器写一份基础库。返回 True 表示新建了。

    没给 tree 就不写：build() 只有拿到 ctx 才按盘过滤条目，拿不到扫描数据时
    写出的是「各盘条目混在一起、路径还留着 %DRIVE%」的通用库；而 ensure() 见到
    文件存在就直接返回 —— 这份错的库会被一直用下去。等这个盘扫出树来，下次
    启动会补上。
    """
    if os.path.exists(path):
        return False
    if not tree:
        sys.stderr.write("[warn] 还没有扫描树，跳过 %s 的生成\n" % path)
        return False
    ctx = pathvars.context(pathvars.home_of(tree),
                           tree.get("root") or "C:",
                           pathvars.sysdrive_of(tree))
    wr(path, build(ctx))
    return True


def load(path, tree):
    """读知识库并按本次扫描展开占位符。

    返回 (P, exact, names, paths)；P 是展开后的原始结构，便于统计条数。
    """
    raw = rd(path)
    if raw is None:
        return None, {}, [], []
    home = pathvars.home_of(tree)
    P = pathvars.expand(raw, pathvars.context(
        home, (tree or {}).get("root", "C:"), pathvars.sysdrive_of(tree)))
    exact = {}
    for o in P.get("exact", []):
        exact[o["p"]] = o
    names = [(re.compile(o["re"], js_flags(o.get("fl"))), o) for o in P.get("names", [])]
    paths = [(re.compile(o["src"], js_flags(o.get("fl"))), o) for o in P.get("paths", [])]
    return P, exact, names, paths


# ---------------------------------------------------------------------------
# 对账：复刻前端 purposeOf()
# ---------------------------------------------------------------------------

def classify(path, name, exact, names, paths):
    """返回 (kind, source)。kind: exact / rule-name / rule-path / inherited / none。

    必须完全照抄运行时的顺序和细节，否则对账结果会和页面显示对不上：
      - 盘根要同时试 "C:" 和 "C:\" 两种形态才能命中 exact
      - 名字规则拿的是节点名，路径规则拿的是完整路径
      - 祖先继承同样要处理「父路径正好是盘根」的情况
    """
    # 盘根的两种写法都要试：知识库里写 %DRIVE%: 展开得到 "C:"，
    # 写 %DRIVE%:\ 则得到 "C:\"；而树里的盘根节点路径是 "C:"。
    # 只补反斜杠而不试原形的话，"%DRIVE%:" 这条永远命不中。
    cands = [path]
    if len(path) == 2 and path[1] == ":":
        cands.append(path + BS)
    for c in cands:
        if c in exact:
            return "exact", c

    self = path

    for rx, _o in names:
        if rx.search(name):
            return "rule-name", None

    for rx, _o in paths:
        if rx.search(self):
            return "rule-path", None

    p = self
    for _ in range(MAX_ANCESTOR):
        j = p.rfind(BS)
        if j <= 0:
            break
        q = p[:j]
        if len(q) == 2 and q[1] == ":":
            q = q + BS
        p = q
        if p in exact:
            return "inherited", p

    return "none", None


def walk(node, path, depth, out):
    for k in node.get("c", []):
        name = k.get("n", "")
        base = path if path.endswith(BS) else path + BS
        child = base + name
        out.append({
            "p": child,
            "n": name,
            "d": depth + 1,
            "f": 1 if k.get("f") == 1 else 0,
            "b": float(k.get("b", 0)),
        })
        walk(k, child, depth + 1, out)


def snapshot(tree, P):
    """把整棵树过一遍，给出每个节点的兜底档次。

    返回 {"nodes": [...], "stats": {...}, "gaps": [...]}。
    gaps 只收「没有精确匹配」的节点，按体积降序 —— 那就是 ticket 的候选。
    """
    _P, exact, names, paths = load_index(P)
    nodes = []
    root = (tree or {}).get("tree")
    rootp = (tree or {}).get("root") or "C:"
    if root:
        nodes.append({"p": rootp, "n": root.get("n", rootp), "d": 0,
                      "f": 1 if root.get("f") == 1 else 0,
                      "b": float(root.get("b", 0))})
        walk(root, rootp, 0, nodes)

    by_kind = {"exact": 0, "rule-name": 0, "rule-path": 0, "inherited": 0, "none": 0}
    gaps = []
    for nd in nodes:
        kind, src = classify(nd["p"], nd["n"], exact, names, paths)
        by_kind[kind] = by_kind.get(kind, 0) + 1
        nd["k"] = kind
        if kind != "exact":
            gaps.append({"p": nd["p"], "n": nd["n"], "b": nd["b"],
                         "d": nd["d"], "why": kind, "src": src})
    gaps.sort(key=lambda x: -x["b"])

    return {
        "nodes": nodes,
        "gaps": gaps,
        "stats": {
            "total": len(nodes),
            "exact": by_kind["exact"],
            "noExact": len(nodes) - by_kind["exact"],
            "byKind": by_kind,
            "exactRules": {
                "paths": len(exact),
                "names": len(names),
                "patterns": len(paths),
            },
        },
    }


def load_index(P):
    """从已展开的 P 建索引（snapshot 内部用，避免重复读盘）。"""
    exact = {}
    for o in (P or {}).get("exact", []):
        exact[o["p"]] = o
    names = [(re.compile(o["re"], js_flags(o.get("fl"))), o)
             for o in (P or {}).get("names", [])]
    paths = [(re.compile(o["src"], js_flags(o.get("fl"))), o)
             for o in (P or {}).get("paths", [])]
    return P, exact, names, paths
