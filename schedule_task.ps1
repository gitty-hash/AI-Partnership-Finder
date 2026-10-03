# Registers (or removes) a Windows scheduled task that runs the partnership
# finder every Monday and Thursday at 8 AM Pacific time.
#
#   .\schedule_task.ps1                  # Mon + Thu at 8:00 AM Pacific
#   .\schedule_task.ps1 -Time "07:30"    # different time (still Pacific)
#   .\schedule_task.ps1 -Remove          # delete the schedule
#
# Task Scheduler only understands this computer's local time, so the Pacific
# time is converted to local time when the task is registered. Pacific time
# shifts by an hour with daylight saving and a task can't follow that on its
# own, so re-run this script after the clocks change (March and November) to
# keep it at 8 AM Pacific.
#
# The task runs as you, on this machine, so it reads your .env keys directly.
# If the computer is off at the scheduled time, the run starts as soon as it is
# next available. Each run writes output\leads_<date>.csv and logs\run_<time>.log.
param(
    [string]$Time = "08:00",
    [string]$TimeZoneId = "Pacific Standard Time",   # Windows ID; covers PST and PDT
    [switch]$Remove
)

$TaskName = "WaresportPartnershipFinder"

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName' (if it existed)."
    exit 0
}

# Convert "Monday and Thursday at $Time in $TimeZoneId" to this computer's local
# time (the converted day can shift if the two zones are far enough apart).
$SourceZone = [System.TimeZoneInfo]::FindSystemTimeZoneById($TimeZoneId)
$LocalZone = [System.TimeZoneInfo]::Local
$TimeOfDay = [datetime]::ParseExact($Time, "HH:mm", [System.Globalization.CultureInfo]::InvariantCulture).TimeOfDay

$Days = @()
$LocalTime = $null
foreach ($Day in "Monday", "Thursday") {
    # Next occurrence of that weekday, at the requested time in the source zone.
    $Date = (Get-Date).Date
    while ($Date.DayOfWeek -ne $Day) { $Date = $Date.AddDays(1) }
    $SourceTime = [datetime]::SpecifyKind($Date + $TimeOfDay, [System.DateTimeKind]::Unspecified)
    $Converted = [System.TimeZoneInfo]::ConvertTime($SourceTime, $SourceZone, $LocalZone)
    $Days += $Converted.DayOfWeek
    $LocalTime = $Converted.ToString("HH:mm")
}

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunScript = Join-Path $ProjectDir "run_scheduled.ps1"

# Bake in the full path to python so the task doesn't depend on PATH at run time.
$PythonExe = (Get-Command python -ErrorAction Stop).Source

$Action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$RunScript`" -PythonExe `"$PythonExe`"" `
    -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $Days -At $LocalTime
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Description "Waresport partnership finder: Mondays and Thursdays at $Time $TimeZoneId" -Force | Out-Null

Write-Host "Scheduled '$TaskName' for Monday and Thursday at $Time ($TimeZoneId)."
Write-Host "On this computer ($($LocalZone.Id)) that is: $($Days -join ' and ') at $LocalTime."
Write-Host "Test it now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Remove it with:    .\schedule_task.ps1 -Remove"
