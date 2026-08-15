Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$tvProcess = Get-Process -Id 14176 -ErrorAction SilentlyContinue
if (!$tvProcess) {
    $tvProcess = Get-Process | Where-Object { $_.ProcessName -like "*TeamViewer*" -or $_.ProcessName -like "*teamviewer*" } | Select-Object -First 1
}

Write-Output "=== PROCESS: $($tvProcess.ProcessName) (ID: $($tvProcess.Id)) ==="
Write-Output "=== TITLE: $($tvProcess.MainWindowTitle) ==="

$root = [System.Windows.Automation.AutomationElement]::FromHandle($tvProcess.MainWindowHandle)

# Use TrueCondition static property
$trueCond = [System.Windows.Automation.Condition]::TrueCondition
$allElements = $root.FindAll([System.Windows.Automation.TreeScope]::Subtree, $trueCond)
Write-Output "=== Found $($allElements.Count) total elements ==="
foreach ($el in $allElements) {
    try {
        $name = $el.Current.Name
        if ($name) {
            $ctrlType = $el.Current.ControlType.ProgrammaticName
            Write-Output "  [$ctrlType] $name"
        }
    } catch {}
}

# Text patterns
Write-Output "=== TEXT PATTERNS ==="
foreach ($el in $allElements) {
    try {
        $txtPattern = $el.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern)
        if ($txtPattern) {
            $txtRange = $txtPattern.DocumentRange
            $text = $txtRange.GetText(-1)
            if ($text -and $text.Trim()) {
                Write-Output "  Text: $text"
            }
        }
    } catch {}
}

# Value patterns
Write-Output "=== VALUE PATTERNS ==="
foreach ($el in $allElements) {
    try {
        $valPattern = $el.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        if ($valPattern) {
            $val = $valPattern.Current.Value
            if ($val -and $val.Trim()) {
                Write-Output "  Value: $val"
            }
        }
    } catch {}
}

# LegacyIAccessible
Write-Output "=== LEGACY IACCESSIBLE ==="
foreach ($el in $allElements) {
    try {
        $legacyPattern = $el.GetCurrentPattern([System.Windows.Automation.LegacyIAccessiblePattern]::Pattern)
        if ($legacyPattern) {
            $name = $legacyPattern.Current.Name
            $desc = $legacyPattern.Current.Description
            $val = $legacyPattern.Current.Value
            $def = $legacyPattern.Current.DefaultAction
            if ($name) { Write-Output "  Name: $name" }
            if ($desc) { Write-Output "  Desc: $desc" }
            if ($val) { Write-Output "  Value: $val" }
            if ($def) { Write-Output "  Action: $def" }
        }
    } catch {}
}