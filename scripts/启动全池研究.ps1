param([ValidateSet('Page','Resume','Observer','Stop')][string]$Mode='Page')
$ErrorActionPreference='Stop'
$taskRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$taskPython=Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Project virtual environment is missing. See RUNBOOK.md.' }
Set-Location -LiteralPath $taskRoot
if ($Mode -eq 'Stop') { & $taskPython -m src.watchlist stop-paper; exit }
if ($Mode -eq 'Observer') { & $taskPython -m src.watchlist paper-service --days 30; exit }
if ($Mode -eq 'Resume') { & $taskPython -m src.watchlist run-all --resume; exit }
$taskPort=Get-NetTCPConnection -LocalPort 8512 -State Listen -ErrorAction SilentlyContinue
if (-not $taskPort) {
    Start-Process -FilePath $taskPython -ArgumentList '-m','streamlit','run','pages/12_全池研究.py','--server.headless','true','--server.port','8512','--server.address','127.0.0.1','--browser.gatherUsageStats','false' -WorkingDirectory $taskRoot -WindowStyle Hidden
} else {
    $taskRunning=Get-CimInstance Win32_Process -Filter "ProcessId=$($taskPort[0].OwningProcess)"
    if ($taskRunning.CommandLine -notlike '*12_全池研究.py*' -or $taskRunning.ExecutablePath -notlike "$taskRoot*") { throw 'Port 8512 belongs to another application; it has not been changed.' }
}
Start-Process 'http://127.0.0.1:8512/'
