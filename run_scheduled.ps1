# Runs one full partnership-finder pass and logs the output. Called by the
# Windows scheduled task (see schedule_task.ps1), but also fine to run by hand.
param(
    [string]$PythonExe = "python"
)

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

# A scheduled run should always use the default providers (OpenAI + DuckDuckGo
# + Firecrawl), even if an experiment variable was left set on this machine.
Remove-Item Env:LLM_PROVIDER, Env:SCRAPE_PROVIDER -ErrorAction SilentlyContinue

New-Item -ItemType Directory -Force -Path "logs" | Out-Null
$Stamp = Get-Date -Format "yyyy-MM-dd_HH-mm"
$LogFile = "logs\run_$Stamp.log"

"[$(Get-Date -Format s)] starting run" | Out-File -FilePath $LogFile -Encoding utf8
& $PythonExe main.py *>> $LogFile
"[$(Get-Date -Format s)] finished with exit code $LASTEXITCODE" | Out-File -FilePath $LogFile -Append -Encoding utf8
exit $LASTEXITCODE
