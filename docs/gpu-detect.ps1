# gpu-detect.ps1 - which graphics card is this, and which PyTorch build does it need?
#
# Reference implementation of docs/gpu.md; every studio keeps a copy next to its initialiser.
#
#   . .\gpu-detect.ps1                       # dot-source, then:
#   $gpu = Get-StudioGpu                     # -> @{ vendor; backend; name; gfx; vram_mb; cuda_tag; torch_args }
#   $gpu = Get-StudioGpu -Force amd -Gfx gfx1100
#   Write-StudioGpuJson $gpu "$ROOT\.gpu.json" -Torch "2.12.0+rocm7.14.1"
#
# torch_args is the argument list for `pip install` (index + package specs); print it, run it.

$script:ROCM_WIN_VERSION = "7.14.1"
$script:ROCM_WIN_TORCH   = "2.12.0"
$script:ROCM_WIN_VISION  = "0.27.0"
$script:ROCM_WIN_AUDIO   = "2.11.0"
$script:ROCM_WIN_INDEX   = "https://repo.amd.com/rocm/whl-multi-arch/"

function Get-AmdGfx([string]$Name) {
  # The LLVM target from the adapter's marketing name (docs/gpu.md has the table). "" = unknown.
  $n = $Name.ToLowerInvariant()
  if ($n -match "rx 9070|r9700")                              { return "gfx1201" }
  if ($n -match "rx 9060")                                    { return "gfx1200" }
  if ($n -match "rx 7900|w7900|w7800")                        { return "gfx1100" }
  if ($n -match "rx 7800|rx 7700|w7700")                      { return "gfx1101" }
  if ($n -match "rx 7600|rx 7650")                            { return "gfx1102" }
  if ($n -match "780m|760m")                                  { return "gfx1103" }
  if ($n -match "880m|890m")                                  { return "gfx1150" }
  if ($n -match "8060s|8050s|8040s")                          { return "gfx1151" }
  if ($n -match "rx 6950|rx 6900|rx 6800|w6800")              { return "gfx1030" }
  return ""
}

function Get-AmdSupport([string]$Name, [string]$Gfx) {
  # ok | all (use device-all) | unsupported
  if ($Gfx) { return "ok" }
  $n = $Name.ToLowerInvariant()
  if ($n -match "rx 6700|rx 6750")                            { return "all" }
  if ($n -match "rx 6[0-6]\d\d|rx 5\d\d\d|vega|rx 5\d0|radeon r") { return "unsupported" }
  return "all"
}

function Get-StudioGpu {
  param([string]$Force = "", [string]$Gfx = "")
  $gpu = @{ vendor = "cpu"; backend = "cpu"; name = ""; gfx = ""; vram_mb = 0; driver = ""; cuda_tag = ""; torch_args = @(); note = "" }

  # 1. NVIDIA
  $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
  if (-not $smi -and (Test-Path "$env:SystemRoot\System32\nvidia-smi.exe")) { $smi = "$env:SystemRoot\System32\nvidia-smi.exe" }
  if (($Force -eq "" -or $Force -eq "nvidia") -and $smi) {
    $line = & $smi --query-gpu=driver_version,name,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1
    if ($line) {
      $p = $line -split ","
      $gpu.vendor = "nvidia"; $gpu.backend = "cuda"; $gpu.driver = $p[0].Trim(); $gpu.name = $p[1].Trim(); $gpu.vram_mb = [int]$p[2].Trim()
      $major = [int]($gpu.driver -split "\.")[0]
      $gpu.cuda_tag = if ($major -ge 580) { "cu130" } elseif ($major -ge 528) { "cu126" } else { "cu126" }
      $gpu.torch_args = @("torch", "torchvision", "torchaudio", "--index-url", "https://download.pytorch.org/whl/$($gpu.cuda_tag)")
      if ($Force -eq "nvidia" -or $Force -eq "") { return $gpu }
    }
  }

  # 2. AMD
  if ($Force -eq "" -or $Force -eq "amd") {
    $adapters = @()
    try { $adapters = @(Get-CimInstance Win32_VideoController -ErrorAction Stop | Where-Object { $_.Name -match "AMD|Radeon" }) } catch { $adapters = @() }
    if ($Force -eq "amd" -and -not $adapters) { $adapters = @(@{ Name = "AMD (forced)"; AdapterRAM = 0 }) }
    if ($adapters) {
      $a = $adapters | Sort-Object { $_.AdapterRAM } -Descending | Select-Object -First 1
      $gpu.vendor = "amd"; $gpu.backend = "rocm"; $gpu.name = [string]$a.Name
      $gpu.gfx = if ($Gfx) { $Gfx } else { Get-AmdGfx $gpu.name }
      $gpu.vram_mb = Get-AdapterVramMb $gpu.name $a
      $support = Get-AmdSupport $gpu.name $gpu.gfx
      if ($support -eq "unsupported") {
        $gpu.backend = "cpu"; $gpu.torch_args = @("torch", "torchvision", "torchaudio", "--index-url", "https://download.pytorch.org/whl/cpu")
        $gpu.note = "$($gpu.name) is not in AMD's PyTorch-on-Windows wheels (RDNA 3 / RDNA 4 / RX 6800+ only) - installing the CPU build"
        return $gpu
      }
      $dev = if ($gpu.gfx) { "device-$($gpu.gfx)" } else { "device-all" }
      $gpu.torch_args = @("--index-url", $script:ROCM_WIN_INDEX,
        "torch[$dev]==$($script:ROCM_WIN_TORCH)+rocm$($script:ROCM_WIN_VERSION)",
        "torchvision[$dev]==$($script:ROCM_WIN_VISION)+rocm$($script:ROCM_WIN_VERSION)",
        "torchaudio==$($script:ROCM_WIN_AUDIO)+rocm$($script:ROCM_WIN_VERSION)")
      if (-not $gpu.gfx) { $gpu.note = "unknown Radeon model '$($gpu.name)': installing the multi-architecture build (device-all, larger download)" }
      return $gpu
    }
  }

  # 3. CPU
  $gpu.torch_args = @("torch", "torchvision", "torchaudio", "--index-url", "https://download.pytorch.org/whl/cpu")
  $gpu.note = "no NVIDIA or AMD graphics card found - installing the CPU build; generation will be very slow"
  return $gpu
}

function Get-AdapterVramMb([string]$Name, $Adapter) {
  # Win32_VideoController.AdapterRAM is a 32-bit field (caps at 4 GB); the registry has the real figure.
  try {
    $keys = Get-ChildItem "HKLM:\SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}" -ErrorAction Stop |
      Where-Object { $_.PSChildName -match "^\d{4}$" }
    foreach ($k in $keys) {
      $p = Get-ItemProperty $k.PSPath -ErrorAction SilentlyContinue
      if ($p.DriverDesc -and $Name -like "*$($p.DriverDesc)*" -or $p.DriverDesc -eq $Name) {
        if ($p.'HardwareInformation.qwMemorySize') { return [int]([int64]$p.'HardwareInformation.qwMemorySize' / 1MB) }
      }
    }
  } catch {}
  if ($Adapter.AdapterRAM) { return [int]([int64]$Adapter.AdapterRAM / 1MB) }
  return 0
}

function Write-StudioGpuJson($Gpu, [string]$Path, [string]$Torch = "") {
  $o = [ordered]@{ vendor = $Gpu.vendor; name = $Gpu.name; gfx = $Gpu.gfx; vram_mb = $Gpu.vram_mb; backend = $Gpu.backend
                   torch = $Torch; driver = $Gpu.driver; platform = "windows"; detected = (Get-Date).ToString("s") }
  ($o | ConvertTo-Json -Compress) | Set-Content -Path $Path -Encoding UTF8
}

function Show-StudioGpu($Gpu) {
  $what = switch ($Gpu.backend) { "cuda" { "PyTorch CUDA $($Gpu.cuda_tag)" } "rocm" { "PyTorch ROCm $($script:ROCM_WIN_VERSION) ($(if ($Gpu.gfx) { $Gpu.gfx } else { 'device-all' }))" } default { "PyTorch CPU build" } }
  Write-Host ("       GPU: {0}  {1} MB  ->  {2}" -f ($(if ($Gpu.name) { $Gpu.name } else { "none" }), $Gpu.vram_mb, $what))
  if ($Gpu.note) { Write-Host "       note: $($Gpu.note)" }
}
