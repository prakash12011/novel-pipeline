# Start the Chrome that flow/gen.mjs drives: your Flow sign-in, with remote debugging on port 9222.
#   powershell -File flow\start-chrome.ps1
# The profile is the one already signed in to Flow for the stickman project. Close any Chrome
# window using it first: two Chromes cannot share one profile.
param(
    [string]$Profile = "D:\Automations\stickman-projects\.flow-chrome-profile",
    [string]$Url = "https://labs.google/fx/tools/flow"
)
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"
if (-not (Test-Path $chrome)) { throw "Chrome not found at $chrome" }
if (-not (Test-Path $Profile)) { throw "Profile not found at $Profile" }
Start-Process $chrome -ArgumentList "--remote-debugging-port=9222", "--user-data-dir=`"$Profile`"", $Url
Write-Output "Chrome started. Sign in if asked, open the episode's Flow project, then run flow/preflight.mjs."
