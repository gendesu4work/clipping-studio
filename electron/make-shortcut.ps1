# Creates "Creator Studio" shortcuts (Desktop + Start menu) that open the Electron app.
$root = Split-Path -Parent $PSScriptRoot
$electron = Join-Path $root "node_modules\electron\dist\electron.exe"
if (-not (Test-Path $electron)) { Write-Error "Run 'npm install' first."; exit 1 }

$shell = New-Object -ComObject WScript.Shell
$targets = @(
  (Join-Path ([Environment]::GetFolderPath("Desktop")) "Creator Studio.lnk"),
  (Join-Path ([Environment]::GetFolderPath("Programs")) "Creator Studio.lnk"),
  (Join-Path $root "Creator Studio.lnk")
)
foreach ($lnk in $targets) {
  $s = $shell.CreateShortcut($lnk)
  $s.TargetPath = $electron
  $s.Arguments = "`"$root`""
  $s.WorkingDirectory = $root
  $s.IconLocation = (Join-Path $root "studio.ico")
  $s.Description = "Creator Studio"
  $s.Save()
  Write-Output "Created $lnk"
}
