<#
  AI Studio Hub - install.ps1: from a bare Windows PC to the hub running, in one command.

    irm https://raw.githubusercontent.com/jrlabanza/ai-studio-hub/main/install.ps1 | iex
    & ([scriptblock]::Create((irm https://raw.githubusercontent.com/jrlabanza/ai-studio-hub/main/install.ps1))) -Skip forge
    "Install AI Studio Hub.bat"     double-click: runs this file (or the latest one from GitHub)

    -Dir DIR       where the hub lives (default: this checkout, else %USERPROFILE%\AI\ai-studio-hub)
    -Skip TOOL     leave studios out: image, voice, video, music, forge (comma-separated)
    -NoStart       set everything up, but do not start the hub
    -NoModels      environments only, where a studio's setup allows it (Video Studio's pack included)
    -NoBrowser     do not open the browser at the end
    -DryRun        print every step it would take and change nothing

  With "irm | iex" the options can also come from environment variables: AI_HUB_DIR, AI_HUB_SKIP,
  AI_HUB_NO_START=1, AI_HUB_NO_MODELS=1, AI_HUB_NO_BROWSER=1, AI_HUB_DRY_RUN=1.

  Steps: 1 checks and graphics card  2 Git, Python 3.11 and the VC++ runtime (winget)  3 the code
         4 the studios, each with its own Windows initializer (own Python environment, PyTorch for the card,
           models)  5 the hub (Initialize AI Studio Hub.bat)  6 start the hub + Video Studio's model pack
  Safe to re-run: every step checks first, and the studios' initializers skip what is done.
  On Windows the studios run natively (each in its own .venv); Docker is not needed.
#>
param(
  [string]$Dir = "",
  [string[]]$Skip = @(),
  [switch]$NoStart,
  [switch]$NoModels,
  [switch]$NoBrowser,
  [switch]$DryRun
)

$script:AsFile = [bool]$PSCommandPath      # empty under "irm | iex": never "exit" then, it would close the window

function Install-AIStudioHub {
  $ErrorActionPreference = "Continue"
  $ProgressPreference = "SilentlyContinue"
  try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch {}
  $RepoUrl = "https://github.com/jrlabanza/ai-studio-hub.git"
  $RawUrl = "https://raw.githubusercontent.com/jrlabanza/ai-studio-hub/main"
  $ToolIds = @("image", "tts", "video", "music", "forge")

  # ---- options (parameters, else environment variables for "irm | iex")
  if (-not $Dir -and $env:AI_HUB_DIR) { $Dir = $env:AI_HUB_DIR }
  $skipList = @($Skip)
  if ($skipList.Count -eq 0 -and $env:AI_HUB_SKIP) { $skipList = @($env:AI_HUB_SKIP) }
  $dry = [bool]$DryRun -or $env:AI_HUB_DRY_RUN -eq "1"
  $start = -not ([bool]$NoStart -or $env:AI_HUB_NO_START -eq "1")
  $models = -not ([bool]$NoModels -or $env:AI_HUB_NO_MODELS -eq "1")
  $browser = -not ([bool]$NoBrowser -or $env:AI_HUB_NO_BROWSER -eq "1")
  $skipped = @{}
  foreach ($s in ($skipList -join ",") -split "[,\s]+") {
    if (-not $s) { continue }
    switch -Regex ($s.ToLower()) {
      "^(image|images)$" { $skipped["image"] = $true }
      "^(tts|voice|qwen-tts)$" { $skipped["tts"] = $true }
      "^(video|lumen)$" { $skipped["video"] = $true }
      "^(music|yue2|yue)$" { $skipped["music"] = $true }
      "^forge$" { $skipped["forge"] = $true }
      default { throw "unknown studio for -Skip: $s (image, voice, video, music, forge)" }
    }
  }

  function Say($n, $msg) { Write-Host ""; Write-Host "[$n] $msg" -ForegroundColor White }
  function Ok($msg) { Write-Host "      $msg" }
  function Note($msg) { Write-Host "      $msg" -ForegroundColor Yellow }
  function Bad($msg) { Write-Host "      $msg" -ForegroundColor Red }
  # Step "what it does" { the commands }: runs them, or in a dry run only says what it would do.
  # Returns $true when the last native command exited with 0.
  function Step([string]$Display, [scriptblock]$Action) {
    if ($dry) { Write-Host "      would run: $Display" -ForegroundColor Cyan; return $true }
    $global:LASTEXITCODE = 0
    & $Action | Out-Host
    return ($LASTEXITCODE -eq 0)
  }
  function Update-PathEnv {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
  }
  function Test-Py311 {
    try { & py -3.11 -c "import sys" 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) } catch { return $false }
  }
  # A .bat run to its end without stopping: "<nul" answers its closing "pause"
  function Invoke-Bat([string]$Bat, [string]$BatArgs, [string]$WorkDir) {
    $line = "`"$Bat`" $BatArgs".TrimEnd()
    if ($dry) { Write-Host "      would run: $line" -ForegroundColor Cyan; return 0 }
    $p = Start-Process -FilePath $env:ComSpec -ArgumentList "/d /s /c `"$line <nul`"" -WorkingDirectory $WorkDir -NoNewWindow -Wait -PassThru
    return $p.ExitCode
  }
  function Get-Json([string]$Url, [int]$Timeout = 5) {
    try { return Invoke-RestMethod -Uri $Url -TimeoutSec $Timeout } catch { return $null }
  }

  Write-Host ""; Write-Host "  AI Studio Hub - setup"; Write-Host "  ---------------------"
  if ($dry) { Write-Host "  (dry run: nothing is changed, every step is only printed)" }

  # ---- 1. Checks and graphics card --------------------------------------------
  Say "1/6" "Checks and graphics card"
  if ($env:OS -ne "Windows_NT") { throw "this installer is for Windows; on Linux use install.sh (README: Quick start)" }
  $os = $null; try { $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop } catch {}
  Ok ("system: " + $(if ($os) { $os.Caption } else { "Windows" }))
  $ramMb = 0
  try { $ramMb = [int]((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1MB) } catch {}
  Ok ("memory: {0} GB" -f [math]::Round($ramMb / 1024))
  $gpu = @{ vendor = "none"; name = ""; vram = 0; cc = ""; driver = "" }
  if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $line = & nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits 2>$null | Select-Object -First 1
    if ($LASTEXITCODE -eq 0 -and $line) {
      $f = $line -split ","
      $gpu.vendor = "nvidia"; $gpu.name = $f[0].Trim(); $gpu.vram = [int]($f[1].Trim() -replace "[^0-9]", ""); $gpu.driver = $f[2].Trim()
      $cc = & nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>$null | Select-Object -First 1
      if ($LASTEXITCODE -eq 0 -and "$cc".Trim() -match "^\d+\.\d+$") { $gpu.cc = "$cc".Trim() }
    }
  }
  if ($gpu.vendor -eq "none") {
    $vc = @(); try { $vc = Get-CimInstance Win32_VideoController -ErrorAction Stop } catch {}
    $amd = $vc | Where-Object { $_.Name -match "AMD|Radeon" } | Select-Object -First 1
    $nv = $vc | Where-Object { $_.Name -match "NVIDIA" } | Select-Object -First 1
    if ($nv) { $gpu.vendor = "nvidia-nodriver"; $gpu.name = $nv.Name }
    elseif ($amd) { $gpu.vendor = "amd"; $gpu.name = $amd.Name }
  }
  switch ($gpu.vendor) {
    "nvidia" { Ok ("graphics card: {0}, {1} GB VRAM{2}, driver {3} -> CUDA builds of PyTorch" -f $gpu.name, [math]::Round($gpu.vram / 1024), $(if ($gpu.cc) { ", compute capability " + $gpu.cc } else { "" }), $gpu.driver) }
    "nvidia-nodriver" { Note ("graphics card: {0}, but nvidia-smi does not answer - install or update the NVIDIA driver (nvidia.com/drivers), then run this again" -f $gpu.name) }
    "amd" { Ok ("graphics card: {0} (AMD) -> AMD's PyTorch-on-ROCm wheels where the studio supports them" -f $gpu.name) }
    default { Note "graphics card: no NVIDIA or AMD card found - the studios install their CPU builds, which are slow or unusable for most models" }
  }

  # ---- 2. Git, Python 3.11, VC++ runtime ---------------------------------------
  Say "2/6" "Git, Python 3.11 and the Visual C++ runtime"
  $winget = Get-Command winget -ErrorAction SilentlyContinue
  function Install-Winget([string]$Id, [string]$What) {
    if (-not $winget -and -not $dry) {
      throw "$What is missing and winget is not available. Install ""App Installer"" from the Microsoft Store (it brings winget), or install $What yourself, then run this again"
    }
    Ok "installing $What (winget $Id)"
    $okay = Step "winget install --id $Id -e --source winget --accept-package-agreements --accept-source-agreements --silent" {
      & winget install --id $Id -e --source winget --accept-package-agreements --accept-source-agreements --silent
    }
    Update-PathEnv
    return $okay
  }
  if (Get-Command git -ErrorAction SilentlyContinue) { Ok ("git: " + ((& git --version) -replace "git version ", "")) }
  else {
    Install-Winget "Git.Git" "Git" | Out-Null
    if (-not $dry -and -not (Get-Command git -ErrorAction SilentlyContinue)) {
      $gp = Join-Path $env:ProgramFiles "Git\cmd"; if (Test-Path "$gp\git.exe") { $env:Path = "$gp;$env:Path" }
    }
    if (-not $dry -and -not (Get-Command git -ErrorAction SilentlyContinue)) { throw "Git did not install - install it from https://git-scm.com/download/win and run this again" }
  }
  # Voice Studio's setup asks for exactly Python 3.11 (py -3.11); the hub and the other studios accept it too.
  if (Test-Py311) { Ok ("python: " + (& py -3.11 -c "import sys; print(sys.version.split()[0], sys.executable)")) }
  else {
    Install-Winget "Python.Python.3.11" "Python 3.11" | Out-Null
    if (-not $dry -and -not (Test-Py311)) { throw "Python 3.11 did not install - install it from https://www.python.org/downloads/windows/ (tick ""py launcher""), then run this again" }
  }
  $vcOk = $false
  foreach ($k in @("HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\X64", "HKLM:\SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\X64")) {
    try { if ((Get-ItemProperty -Path $k -ErrorAction Stop).Installed -eq 1) { $vcOk = $true } } catch {}
  }
  if ($vcOk) { Ok "Visual C++ runtime: present (PyTorch needs it)" }
  else { if (-not (Install-Winget "Microsoft.VCRedist.2015+.x64" "the Visual C++ runtime")) { Note "the Visual C++ runtime did not install - PyTorch may fail to load until it is (aka.ms/vs/17/release/vc_redist.x64.exe)" } }

  # ---- 3. The code ---------------------------------------------------------------
  Say "3/6" "The code"
  $env:GIT_TERMINAL_PROMPT = "0"      # a repository you cannot access fails at once instead of asking for a password
  $env:GCM_INTERACTIVE = "never"      # ... and Git Credential Manager does not pop up a sign-in window for it
  function Test-Hub([string]$d) { return ($d -and (Test-Path (Join-Path $d "hub\__init__.py")) -and (Test-Path (Join-Path $d "Initialize AI Studio Hub.bat"))) }
  $selfDir = if ($script:AsFile) { Split-Path -Parent $PSCommandPath } else { "" }
  if ($Dir) { $Root = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Dir) }
  elseif (Test-Hub $selfDir) { $Root = $selfDir }
  else { $Root = Join-Path $env:USERPROFILE "AI\ai-studio-hub" }
  if (Test-Hub $Root) { Ok "using the checkout in $Root (left as it is - 'git pull' there to update)" }
  elseif ((Test-Path $Root) -and (Get-ChildItem -Force $Root -ErrorAction SilentlyContinue | Select-Object -First 1)) {
    throw "$Root exists and is not an AI Studio Hub checkout - pick another folder with -Dir"
  }
  else {
    Ok "cloning $RepoUrl into $Root"
    if (-not (Step "git -c core.longpaths=true clone $RepoUrl `"$Root`"" { & git -c core.longpaths=true clone $RepoUrl $Root })) { throw "git clone failed - check your internet connection" }
    Step "git -C `"$Root`" config core.longpaths true" { & git -C $Root config core.longpaths true } | Out-Null
  }

  # Which studios are where: the hub's own lookup (hub\config.py tool_dir) - the answer the hub will use.
  $names = @{ image = "Image Studio"; tts = "Voice Studio"; video = "Video Studio"; music = "Music Studio"; forge = "Forge Studio" }
  $subs = @{ image = "Image gen"; tts = "qwen tts"; video = "video-gen"; music = "Yue2"; forge = "forge" }
  $scanPy = @'
import sys
sys.path.insert(0, sys.argv[1])
from hub.config import DEFAULT_TOOLS, tool_dir, tool_dir_candidates
from hub.tools import TOOLS
for t in TOOLS.values():
    d = tool_dir(t.id)
    co = t.is_checkout(d)
    if not co:
        for c in tool_dir_candidates(t.id):
            if t.is_checkout(c):
                d, co = c, True
                break
    inst = co and t.installed(d)[0]
    mod = co and t.model_present(d)[0]
    print("\t".join([t.id, DEFAULT_TOOLS[t.id]["dir"], str(d), str(int(co)), str(int(bool(inst))), str(int(bool(mod)))]))
'@
  function Get-Studios {
    $table = @{}
    if (-not (Test-Hub $Root) -or -not (Test-Py311)) { return $table }
    $out = $scanPy | & py -3.11 -I - $Root 2>$null
    foreach ($l in $out) {
      $f = "$l" -split "`t"
      if ($f.Count -ge 6) { $table[$f[0]] = @{ sub = $f[1]; dir = $f[2]; checkout = $f[3] -eq "1"; installed = $f[4] -eq "1"; models = $f[5] -eq "1" } }
    }
    return $table
  }
  $T = Get-Studios
  $gone = @{}
  foreach ($id in $ToolIds) {
    if ($skipped[$id]) { Ok "$($names[$id]): skipped (-Skip)"; continue }
    if ($T[$id] -and $T[$id].checkout) { Ok "$($names[$id]): $($T[$id].dir)"; continue }
    Ok "$($names[$id]): fetching the '$($subs[$id])' submodule"
    if (-not (Step "git -C `"$Root`" submodule update --init --recursive -- `"$($subs[$id])`"" { & git -c core.longpaths=true -C $Root submodule update --init --recursive -- $subs[$id] })) {
      Note "could not fetch $($names[$id]) (a private repository you have no access to, or no internet) - the hub runs without it"
      $gone[$id] = $true
    }
  }
  if (-not $dry) { $T = Get-Studios }
  $studios = @()
  foreach ($id in $ToolIds) {
    if ($skipped[$id] -or $gone[$id]) { continue }
    if (-not $dry -and -not ($T[$id] -and $T[$id].checkout)) { Note "$($names[$id]): no checkout found after fetching - left out"; $gone[$id] = $true; continue }
    if (-not $T[$id]) { $T[$id] = @{ sub = $subs[$id]; dir = (Join-Path $Root $subs[$id]); checkout = $false; installed = $false; models = $false } }
    $studios += $id
  }

  # ---- 4. The studios --------------------------------------------------------------
  Say "4/6" "The studios (each with its own Windows initializer: Python environment, PyTorch for the card, models)"
  # Each studio's own script and the switches that keep it from asking questions.
  $inits = @{
    image = @{ bat = "Initialize.bat"; args = "--yes"; nomodels = "--yes --skip-models" }
    tts   = @{ bat = "initialize.bat"; args = ""; nomodels = "-NoModels" }
    video = @{ bat = "initialize.bat"; args = ""; nomodels = "" }                # its model pack comes on first start
    music = @{ bat = "initialize.bat"; args = ""; nomodels = $null }             # always fetches the YuE2 weights
    forge = @{ bat = "initialize.bat"; args = "-Essentials -SkipShortcuts"; nomodels = "-NoModels -SkipShortcuts" }   # the hub is the shortcut
  }
  $failed = @()
  foreach ($id in $studios) {
    $i = $inits[$id]; $d = $T[$id].dir; $bat = Join-Path $d $i.bat
    Write-Host ""; Write-Host "  --- $($names[$id])  ($bat)"
    if (-not $dry -and -not (Test-Path $bat)) { Bad "no $($i.bat) in $d"; $failed += $names[$id]; continue }
    $a = $i.args
    if (-not $models) {
      if ($null -eq $i.nomodels) { Note "$($names[$id])'s setup has no option to skip its models; it downloads them" }
      else { $a = $i.nomodels }
    }
    $rc = Invoke-Bat $bat $a $d
    if ($rc -ne 0) { Bad "$($names[$id]) setup did not complete (exit code $rc) - the others carry on; re-run the installer to retry"; $failed += $names[$id] }
  }
  if ($studios.Count -eq 0) { Note "no studios to set up" }
  if (-not $dry) {
    # several initializers end with "pause" and report 0 either way: the hub's own check is the judge
    $T = Get-Studios
    foreach ($id in $studios) {
      if ($T[$id] -and -not $T[$id].installed -and $failed -notcontains $names[$id]) { Bad "$($names[$id]) is not set up after its initializer - see its messages above"; $failed += $names[$id] }
    }
  }

  # ---- 5. The hub ---------------------------------------------------------------------
  Say "5/6" "The hub (its .venv and a check of every studio)"
  $hubBat = Join-Path $Root "Initialize AI Studio Hub.bat"
  if ((Invoke-Bat $hubBat "" $Root) -ne 0) { throw "the hub's setup failed (Initialize AI Studio Hub.bat)" }

  # ---- 6. Start + Video Studio's model pack ------------------------------------------
  Say "6/6" "Start"
  $hubPort = 7900
  try { $sj = Get-Content -Raw (Join-Path $Root "data\settings.json") -ErrorAction Stop | ConvertFrom-Json; if ($sj.hub_port) { $hubPort = [int]$sj.hub_port } } catch {}
  $url = "http://localhost:$hubPort"; $api = "http://127.0.0.1:$hubPort"
  $state = Get-Json "$api/api/state" 3
  if (-not $start) { Ok "not started (-NoStart). Start it with: ""$Root\Start AI Studio Hub.bat""" }
  elseif ($state) {
    $other = "$($state.app.root)"
    if ($other -and ([IO.Path]::GetFullPath($other).TrimEnd("\") -ne [IO.Path]::GetFullPath($Root).TrimEnd("\"))) {
      Note "another AI Studio Hub ($other) is already running on port $hubPort - close its window and run this again to start this one"
      $start = $false
    } else { Ok "AI Studio Hub is already running at $url" }
  }
  else {
    $startBat = Join-Path $Root "Start AI Studio Hub.bat"
    if ($dry) { Write-Host "      would run: `"$startBat`" --no-browser   (in its own window)" -ForegroundColor Cyan }
    else {
      Start-Process -FilePath $env:ComSpec -ArgumentList "/d /s /c `"`"$startBat`" --no-browser`"" -WorkingDirectory $Root | Out-Null
      $up = $false
      for ($n = 0; $n -lt 90 -and -not $up; $n++) { Start-Sleep -Seconds 1; $up = [bool](Get-Json "$api/api/state" 2) }
      if (-not $up) { throw "the hub did not answer on $url within 90 s - see the ""AI Studio Hub"" window" }
      Ok "AI Studio Hub is running at $url (its own window; closing that window stops the hub and the studios it started)"
    }
  }

  Write-Host ""; Write-Host "  --- Video Studio model pack"
  if ($gpu.vendor -eq "nvidia") { Ok ("card: {0}{1}, {2} GB RAM" -f $gpu.name, $(if ($gpu.cc) { " (compute capability " + $gpu.cc + ")" } else { "" }), [math]::Round($ramMb / 1024)) }
  elseif ($gpu.vendor -eq "amd") { Ok ("card: {0} (ROCm), {1} GB RAM" -f $gpu.name, [math]::Round($ramMb / 1024)) }
  # What Video Studio will choose: its own models_catalog.recommend_variant with this machine's card and memory
  $rec = ""; $recLabel = ""; $recGb = ""
  $v = $T["video"]
  if ($v -and (Test-Path (Join-Path $v.dir "backend\app\models_catalog.py")) -and (Test-Py311)) {
    $backend = if ($gpu.vendor -eq "amd") { "rocm" } else { "cuda" }
    $gfx = ""
    try { $gj = Get-Content -Raw (Join-Path $v.dir ".gpu.json") -ErrorAction Stop | ConvertFrom-Json; if ($gj.backend) { $backend = $gj.backend }; if ($gj.gfx) { $gfx = $gj.gfx } } catch {}
    $prevPy = @'
import sys
a = ["" if x == "-" else x for x in sys.argv]   # "-" stands for an empty value (PowerShell 5.1 drops empty arguments)
sys.path.insert(0, a[1])
from app.models_catalog import FILES, VARIANTS, recommend_variant, variant_file_ids
v = recommend_variant("h3", int(a[2] or 0), int(a[3] or 0), a[4], a[5], a[6])
print(f"{v}|{VARIANTS[v].label}|{sum(FILES[f].size for f in variant_file_ids(v)) / 1e9:.1f}")
'@
    $cc = if ($gpu.cc) { $gpu.cc } else { "-" }
    $gfxArg = if ($gfx) { $gfx } else { "-" }
    $p = $prevPy | & py -3.11 -I - (Join-Path $v.dir "backend") "$($gpu.vram)" "$ramMb" $cc $backend $gfxArg 2>$null | Select-Object -First 1
    if ($p) { $rec, $recLabel, $recGb = "$p" -split "\|" }
  }
  if ($skipped["video"] -or $gone["video"]) { }
  elseif (-not $models) { Ok ("Video Studio: no model pack requested (-NoModels)" + $(if ($rec) { "; it would pick $rec" } else { "" })) }
  elseif ($v -and $v.models) { Ok ("Video Studio already has a model pack" + $(if ($rec) { " (for this card it recommends $rec)" } else { "" }) + " - more in Video Studio > Models") }
  elseif ($v -and (Test-Path (Join-Path $v.dir "data\settings.json")) -and ((Get-Content -Raw (Join-Path $v.dir "data\settings.json")) -match '"auto_download_models":\s*false')) {
    Note "Video Studio's automatic model download is turned off - pick a pack in Video Studio > Models"
  }
  else {
    if ($rec) { Ok "H3 pack for this card: $rec - $recLabel, $recGb GB (Video Studio makes the final choice)" }
    if (-not $start) { Ok "Video Studio downloads its recommended H3 pack the first time it starts" }
    elseif ($dry) {
      Ok "would start Video Studio through the hub, ask it for its recommended H3 pack, and queue only that one:"
      Write-Host "      would run: POST $api/api/tools/video/start" -ForegroundColor Cyan
      Write-Host ("      would run: POST $api/api/models/download  {""tool"":""video"",""variant_id"":""" + $(if ($rec) { $rec } else { "<recommended>" }) + """}") -ForegroundColor Cyan
    }
    else {
      Ok "starting Video Studio to fetch its pack (its first start queues the recommended pack by itself) ..."
      try { Invoke-RestMethod -Method Post -Uri "$api/api/tools/video/start" -TimeoutSec 10 | Out-Null } catch { Note "the hub did not accept the start - open Video Studio once and it downloads its pack" }
      $st = ""; $sw = [Diagnostics.Stopwatch]::StartNew(); $tool = $null
      while ($sw.Elapsed.TotalSeconds -lt 420) {
        $tool = Get-Json "$api/api/tools/video" 5
        if ($tool) { $st = "$($tool.state)" }
        if ($st -eq "running" -or $st -eq "error") { break }
        Start-Sleep -Seconds 5
      }
      if ($st -ne "running") { Note "Video Studio did not come up ($st) - see its log in the hub; it downloads its pack on its first start" }
      else {
        $vport = if ($tool.port) { $tool.port } else { 8765 }
        $sysInfo = Get-Json "http://127.0.0.1:$vport/api/system" 10
        $catalog = Get-Json "http://127.0.0.1:$vport/api/models" 20
        if ($sysInfo -and $sysInfo.system.gpu.name) { Ok ("Video Studio sees: " + $sysInfo.system.gpu.name + $(if ($sysInfo.system.gpu.compute_capability) { " (compute capability " + $sysInfo.system.gpu.compute_capability + ")" } else { "" })) }
        if ($catalog -and $catalog.recommended.h3) {
          if ($catalog.recommended.h3 -ne $rec) { Ok ("Video Studio chose: " + $catalog.recommended.h3) }
          $rec = $catalog.recommended.h3
        }
        if (-not $rec) { Note "could not read Video Studio's recommendation - open Video Studio > Models" }
        else {
          try {
            $body = @{ tool = "video"; variant_id = $rec } | ConvertTo-Json -Compress
            Invoke-RestMethod -Method Post -Uri "$api/api/models/download" -ContentType "application/json" -Body $body -TimeoutSec 60 | Out-Null
            Ok "downloading $rec in the background (only this pack; resumable) - progress in Video Studio > Models or the hub's Models page"
          } catch { Note "the download request was not accepted - start it from Video Studio > Models" }
        }
      }
    }
  }

  # ---- Summary ----------------------------------------------------------------------
  Write-Host ""
  if ($dry) { Write-Host "  Dry run finished - nothing was changed." }
  elseif ($failed.Count) { Write-Host ("  Done, except: " + ($failed -join ", ") + " (see the messages above; re-run the installer to retry).") }
  else { Write-Host "  Done." }
  if ($start) { Write-Host "  AI Studio Hub: $url" } else { Write-Host "  Start it with: ""$Root\Start AI Studio Hub.bat""" }
  Write-Host "  Re-run any time: ""$Root\Install AI Studio Hub.bat""   (or: irm $RawUrl/install.ps1 | iex)"
  Write-Host ""
  if ($start -and $browser -and -not $dry) { Start-Process $url | Out-Null }
  if ($failed.Count) { return 2 }
  return 0
}

$code = 1
try { $code = Install-AIStudioHub | Select-Object -Last 1 }
catch {
  Write-Host ""
  Write-Host ("      " + $_.Exception.Message) -ForegroundColor Red
  Write-Host ""
  Write-Host "  Setup did not complete. Fix the error above and run the installer again - finished steps are skipped."
  Write-Host ""
  $code = 1
}
if ($script:AsFile) { exit $code }
