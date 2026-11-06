# 三角洲复盘助手（测试版）一键安装：检查 Python → 安装依赖 → 下载 FFmpeg 和 PresentMon → 建桌面快捷方式 → 启动
# 每一步都显示正在下载什么、有多大、进度多少；可以重复运行，已经装好的部分会自动跳过。
# 用 Continue：pip 等程序往错误输出写警告时，Windows PowerShell 5.1 在 Stop 模式下会误判为失败并中断
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
$bin = Join-Path $root 'bin'
New-Item -ItemType Directory -Force -Path $bin | Out-Null
# 旧版本留下、新版本已经不用的文件（解压覆盖时不会被删掉）
foreach ($stale in @('fupan\deepseek.py', 'tests\test_deepseek.py')) {
    $p = Join-Path $root $stale
    if (Test-Path $p) { Remove-Item $p -Force }
}

function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Info($text) { Write-Host "    $text" }
function Warn($text) { Write-Host "    $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host ""; Write-Host "安装没有完成：$text" -ForegroundColor Red; exit 1 }

# 测一个网址多久能打开（秒），打不开返回 999
function Measure-Url($url) {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $null = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 8 -ErrorAction Stop
        return [math]::Round($sw.Elapsed.TotalSeconds, 1)
    } catch { return 999 }
}

# 下载文件并显示进度（用 Windows 自带的 curl.exe）；给了 SHA256 就校验，不一致就删掉
function Get-File($url, $out, $sha256) {
    if (Test-Path $out) { Remove-Item $out -Force }
    $curl = Join-Path $env:SystemRoot 'System32\curl.exe'
    if (Test-Path $curl) {
        & $curl -L --fail --retry 2 --retry-delay 2 --connect-timeout 15 -o $out $url
        if ($LASTEXITCODE -ne 0) {
            Warn "下载失败（curl 错误码 $LASTEXITCODE）"
            if (Test-Path $out) { Remove-Item $out -Force }
            return $false
        }
    } else {
        Info "正在下载，请稍候（这台电脑没有 curl，无法显示进度）…"
        try { Invoke-WebRequest -Uri $url -OutFile $out -UseBasicParsing -TimeoutSec 900 -ErrorAction Stop }
        catch {
            Warn "下载失败：$($_.Exception.Message)"
            if (Test-Path $out) { Remove-Item $out -Force }
            return $false
        }
    }
    if ($sha256) {
        $hash = (Get-FileHash -Algorithm SHA256 -Path $out).Hash.ToLower()
        if ($hash -ne $sha256.ToLower()) {
            Warn "文件校验不一致，已删除（可能没下载完整，或者文件被改动过）"
            Remove-Item $out -Force
            return $false
        }
        Info "文件校验通过（SHA256 和官方发布页一致）"
    }
    return $true
}

function Expand-Gzip($src, $dst) {
    $in = [IO.File]::OpenRead($src)
    try {
        $gz = New-Object IO.Compression.GZipStream($in, [IO.Compression.CompressionMode]::Decompress)
        $outFile = [IO.File]::Create($dst)
        try { $gz.CopyTo($outFile) } finally { $outFile.Close(); $gz.Close() }
    } finally { $in.Close() }
}

function Test-FFmpeg($exe) {
    if (-not (Test-Path $exe)) { return $false }
    $filters = & $exe -hide_banner -filters 2>$null | Out-String
    $encs = & $exe -hide_banner -encoders 2>$null | Out-String
    return (($filters -match 'ddagrab') -and ($encs -match 'libvpx'))
}

Write-Host "三角洲复盘助手 安装" -ForegroundColor Cyan
Write-Host "第一次安装要下载约 300 MB，大约 3～10 分钟（看网速）。每一步都会显示进度，请不要关闭这个窗口。"

# 1. Python
Step "1/5 检查 Python（需要 3.10 或更新版本）"
$py = $null
$candidates = @(
    @{ exe = 'py'; args = @('-3.12') },
    @{ exe = 'py'; args = @('-3.11') },
    @{ exe = 'py'; args = @('-3') },
    @{ exe = 'python'; args = @() }
)
foreach ($c in $candidates) {
    if (-not (Get-Command $c.exe -ErrorAction SilentlyContinue)) { continue }
    $a = $c.args
    $ver = & $c.exe @a -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $ver -match '^\d+\.\d+$' -and [version]$ver -ge [version]'3.10') { $py = $c; break }
}
if (-not $py) {
    Warn "没有找到 Python 3.10+。"
    $answer = Read-Host "是否用系统自带的 winget 自动安装 Python 3.12？(Y/N)"
    if ($answer -match '^[Yy]') {
        winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        Fail "Python 已安装，请关闭这个窗口，再双击一次 setup.bat"
    }
    Fail "请到 https://www.python.org/downloads/ 安装 Python 3.12（安装时勾选 Add python.exe to PATH），然后重新运行 setup.bat"
}
Info "使用：$($py.exe) $($py.args -join ' ')（版本 $ver）"

# 2. 独立的 Python 环境和依赖
Step "2/5 安装依赖：界面组件 PySide6（含网页内核）、Windows 文字识别组件等，约 260 MB"
if (-not (Test-Path "$root\.venv\Scripts\python.exe")) {
    Info "创建独立的 Python 环境（只在这个文件夹里，不影响电脑上的其他软件）…"
    $a = $py.args
    & $py.exe @a -m venv "$root\.venv"
    if ($LASTEXITCODE -ne 0) { Fail "创建虚拟环境失败" }
}
$venvPy = "$root\.venv\Scripts\python.exe"
$check = "import PySide6.QtWebEngineWidgets, httpx, keyring, winrt.windows.media.ocr"
& $venvPy -c $check 2>$null
if ($LASTEXITCODE -eq 0) {
    Info "依赖已经装好，跳过"
} else {
    Info "先测一下哪个下载源最快："
    $indexes = @(
        @{ name = '清华大学镜像'; url = 'https://pypi.tuna.tsinghua.edu.cn/simple' },
        @{ name = '阿里云镜像'; url = 'https://mirrors.aliyun.com/pypi/simple' },
        @{ name = 'PyPI 官方'; url = 'https://pypi.org/simple' }
    )
    foreach ($i in $indexes) {
        $i.seconds = Measure-Url ($i.url + '/pyside6/')
        if ($i.seconds -ge 999) { Info ("  {0}：连不上" -f $i.name) } else { Info ("  {0}：{1} 秒" -f $i.name, $i.seconds) }
    }
    $ok = $false
    foreach ($i in ($indexes | Sort-Object { $_.seconds })) {
        if ($i.seconds -ge 999) { continue }
        Info "从 $($i.name) 下载。下面每个文件都会显示进度条，最大的一个约 130 MB。"
        & $venvPy -m pip install -r "$root\requirements.txt" -i $i.url --prefer-binary --timeout 30 --retries 3
        if ($LASTEXITCODE -eq 0) {
            & $venvPy -c $check 2>$null
            if ($LASTEXITCODE -eq 0) { $ok = $true; break }
        }
        Warn "从 $($i.name) 安装没有成功，换下一个下载源重试…"
    }
    if (-not $ok) {
        Fail "依赖安装失败。请检查网络（可以先关掉代理或加速器），再重新双击 setup.bat；已经下载完的文件不会重复下载"
    }
    Info "依赖安装完成"
}

# 3. FFmpeg：录制和生成回放片段都靠它
Step "3/5 准备 FFmpeg（录制和生成回放片段用，约 30 MB）"
$ffmpeg = Join-Path $bin 'ffmpeg.exe'
if (Test-FFmpeg $ffmpeg) {
    Info "FFmpeg 已经可用，跳过"
} else {
    if (Test-Path $ffmpeg) {
        Warn "现有的 FFmpeg 缺少截屏或 VP9 功能，重新下载"
        Remove-Item $ffmpeg -Force
    }
    # 同一个文件（gyan.dev 编译的 FFmpeg 6.1.1）有国内镜像和 GitHub 两个下载地址，都用官方发布页的 SHA256 校验
    $ffGzSha = '8883a3dffbd0a16cf4ef95206ea05283f78908dbfb118f73c83f4951dcc06d77'
    $ffExeSha = '04e1307997530f9cf2fe35cba2ca7e8875ca91da02f89d6c7243df819c94ad00'
    $ffSources = @(
        @{ name = '国内镜像（npmmirror）'; url = 'https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1/ffmpeg-win32-x64.gz' },
        @{ name = 'GitHub'; url = 'https://github.com/eugeneware/ffmpeg-static/releases/download/b6.1.1/ffmpeg-win32-x64.gz' }
    )
    $gz = Join-Path $env:TEMP 'fupan-ffmpeg.gz'
    foreach ($s in $ffSources) {
        Info "从 $($s.name) 下载："
        if (Get-File $s.url $gz $ffGzSha) {
            Info "解压中…"
            Expand-Gzip $gz $ffmpeg
            Remove-Item $gz -Force
            if ((Get-FileHash -Algorithm SHA256 -Path $ffmpeg).Hash.ToLower() -eq $ffExeSha) { break }
            Warn "解压后的文件校验不一致，已删除"
            Remove-Item $ffmpeg -Force
        }
    }
    if (-not (Test-Path $ffmpeg)) {
        # 再试官方网站的压缩包（会随版本更新，没法预先校验，只用 HTTPS 官方地址）
        $zip = Join-Path $env:TEMP 'fupan-ffmpeg.zip'
        $dir = Join-Path $env:TEMP 'fupan-ffmpeg'
        foreach ($url in @('https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip',
                           'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-lgpl.zip')) {
            Info "从 $url 下载（约 100 MB）："
            if (-not (Get-File $url $zip '')) { continue }
            try {
                if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
                Expand-Archive -Path $zip -DestinationPath $dir -Force -ErrorAction Stop
                $found = Get-ChildItem -Path $dir -Recurse -Filter 'ffmpeg.exe' | Select-Object -First 1
                if ($found) {
                    Copy-Item $found.FullName $ffmpeg -Force
                    $probe = Join-Path $found.DirectoryName 'ffprobe.exe'
                    if (Test-Path $probe) { Copy-Item $probe (Join-Path $bin 'ffprobe.exe') -Force }
                    break
                }
            } catch { Warn "解压失败：$($_.Exception.Message)" }
        }
    }
    if (Test-FFmpeg $ffmpeg) {
        Info "FFmpeg 已就绪"
    } else {
        Warn "FFmpeg 没有装好，暂时不能录制和生成片段。可以稍后重新双击 setup.bat 再试，"
        Warn "或者手动下载 https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip ，把里面 bin 文件夹的 ffmpeg.exe 放到 $bin"
    }
}

# 4. PresentMon（英特尔开源的帧率测量工具，可选）
Step "4/5 准备 PresentMon（测帧率用，可选，约 1 MB）"
$pm = Get-ChildItem -Path $bin -Filter 'PresentMon*.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($pm) {
    Info "PresentMon 已存在：$($pm.Name)"
} else {
    $pmFile = Join-Path $bin 'PresentMon-2.6.0-x64.exe'
    $pmUrl = 'https://github.com/GameTechDev/PresentMon/releases/download/v2.6.0/PresentMon-2.6.0-x64.exe'
    if (Get-File $pmUrl $pmFile 'b2a706bc6ad475749e3b7e3409263aa1e6906d45bdcf993f6dbc0f660188f1af') {
        $sig = Get-AuthenticodeSignature $pmFile
        if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'Intel') {
            Remove-Item $pmFile -Force
            Warn "PresentMon 没有英特尔的有效数字签名，已删除。测帧率功能暂时不能用"
        } else {
            Info "PresentMon 已就绪（英特尔数字签名有效）"
        }
    } else {
        Warn "PresentMon 下载失败（不影响录制和复盘，只是不能测帧率）。"
        Warn "可以手动下载 $pmUrl ，放进 $bin"
    }
}

# 5. 桌面快捷方式
Step "5/5 创建桌面快捷方式"
try {
    $shell = New-Object -ComObject WScript.Shell
    $desktop = [Environment]::GetFolderPath('Desktop')
    $old = Join-Path $desktop '复盘助手（测试版）.lnk'
    if (Test-Path $old) { Remove-Item $old -Force }
    $lnk = $shell.CreateShortcut((Join-Path $desktop '三角洲复盘助手（测试版）.lnk'))
    $lnk.TargetPath = "$root\run.bat"
    $lnk.WorkingDirectory = $root
    $lnk.WindowStyle = 7
    if (Test-Path "$root\fupan\web\img\app.ico") { $lnk.IconLocation = "$root\fupan\web\img\app.ico" }
    $lnk.Save()
    Info "已在桌面创建“三角洲复盘助手（测试版）”"
} catch {
    Warn "快捷方式创建失败，可以直接双击 run.bat 启动"
}

Write-Host ""
Write-Host "安装完成，正在启动三角洲复盘助手…" -ForegroundColor Green
Start-Process -FilePath "$root\run.bat" -WorkingDirectory $root
