$N = 5

$baseDir = "C:\colony_system\data\images\A1_repeatability"
$port = "COM3"

$x0 = 8563500
$y0 = 5755000

$profileVel = 200000
$profileAcc = 50000
$profileDec = 50000
$settleS = 0.8

New-Item -ItemType Directory -Force -Path $baseDir | Out-Null

for ($i = 1; $i -le $N; $i++) {
    $runName = "run_{0:D2}" -f $i
    $runDir = Join-Path $baseDir $runName
    New-Item -ItemType Directory -Force -Path $runDir | Out-Null

    Write-Host "=============================="
    Write-Host "开始第 $i 轮 A1 采集"
    Write-Host "结果目录: $runDir"

    # 1) 先回到 A1 起始点
    $pythonExe = "C:\miniconda3\envs\ca\python.exe"
    & $pythonExe C:\colony_system\devices\motion\test_stage_motion.py `
        --port $port `
        --mode abs `
        --x $x0 `
        --y $y0 `
        --profile-vel $profileVel `
        --profile-acc $profileAcc `
        --profile-dec $profileDec

    if ($LASTEXITCODE -ne 0) {
        Write-Host "回到 A1 起始点失败，停止实验"
        exit 1
    }

    Start-Sleep -Seconds 1

    # 2) 执行 A1 扫描
    $pythonExe = "C:\miniconda3\envs\ca\python.exe"
    & $pythonExe C:\colony_system\devices\motion\scan_well_a1_capture_left_start.py `
        --port $port `
        --well A1 `
        --save-dir $runDir `
        --fov-mm 3.0 `
        --overlap 0.10 `
        --profile-vel $profileVel `
        --profile-acc $profileAcc `
        --profile-dec $profileDec `
        --settle-s $settleS `
        --output-json (Join-Path $runDir "scan_manifest.json")

    if ($LASTEXITCODE -ne 0) {
        Write-Host "第 $i 轮扫描失败，停止实验"
        exit 1
    }

    Write-Host "第 $i 轮完成"
    Start-Sleep -Seconds 2
}

Write-Host "=============================="
Write-Host "全部重复采集完成"