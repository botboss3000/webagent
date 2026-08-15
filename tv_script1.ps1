Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$tvProcess = Get-Process -Id 14176 -ErrorAction SilentlyContinue
if (!$tvProcess) {
    $tvProcess = Get-Process | Where-Object { $_.ProcessName -like "*TeamViewer*" -or $_.ProcessName -like "*teamviewer*" } | Select-Object -First 1
}

Write-Output "=== PROCESS: $($tvProcess.ProcessName) (ID: $($tvProcess.Id)) ==="
Write-Output "=== TITLE: $($tvProcess.MainWindowTitle) ==="

$root = [System.Windows.Automation.AutomationElement]::FromHandle($tvProcess.MainWindowHandle)
$cond = New-Object System.Windows.Automation.Condition([System.Windows.Automation.ControlType]::Pane)
$elements = $root.FindAll([System.Windows.Automation.TreeScope]::Subtree, $cond)
Write-Output "=== Found $($elements.Count) pane elements ==="
foreach ($el in $elements) {
    try {
        $name = $el.Current.Name
        if ($name) { Write-Output "  Pane: $name" }
    } catch {}
}

# Also try all control types
$allCond = New-Object System.Windows.Automation.NotCondition($cond)
$allElements = $root.FindAll([System.Windows.Automation.TreeScope]::Subtree, $allCond)
Write-Output "=== All non-pane elements: $($allElements.Count) ==="
foreach ($el in $allElements) {
    try {
        $name = $el.Current.Name  
        if ($name) {
            $ctrlType = $el.Current.ControlType.ProgrammaticName
            Write-Output "  [$ctrlType] $name"
        }
    } catch {}
}