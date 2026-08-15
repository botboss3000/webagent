Add-Type @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public class WinGet {
    [DllImport("user32.dll", CharSet=CharSet.Auto)]
    public static extern int GetWindowText(IntPtr hWnd, StringBuilder lpString, int nMaxCount);
    
    [DllImport("user32.dll")]
    public static extern int GetWindowTextLength(IntPtr hWnd);
    
    [DllImport("user32.dll", SetLastError=true)]
    public static extern IntPtr FindWindowEx(IntPtr hWndParent, IntPtr hWndChildAfter, string lpszClass, string lpszWindow);
    
    [DllImport("user32.dll")]
    public static extern int SendMessage(IntPtr hWnd, uint Msg, IntPtr wParam, StringBuilder lParam);
}
"@

$tvHandle = (Get-Process -Id 14176).MainWindowHandle
$len = [WinGet]::GetWindowTextLength($tvHandle)
$sb = New-Object System.Text.StringBuilder($len + 1)
[WinGet]::GetWindowText($tvHandle, $sb, $len + 1) | Out-Null
Write-Output ("=== WINDOW TEXT (GetWindowText): '" + $sb.ToString() + "' ===")

Write-Output "=== ENUMERATING CHILD WINDOWS ==="
$child = [IntPtr]::Zero
$i = 0
do {
    $child = [WinGet]::FindWindowEx($tvHandle, $child, [NullString]::Value, [NullString]::Value)
    if ($child -ne [IntPtr]::Zero) {
        $clen = [WinGet]::GetWindowTextLength($child)
        if ($clen -gt 0) {
            $csb = New-Object System.Text.StringBuilder($clen + 1)
            [WinGet]::GetWindowText($child, $csb, $clen + 1) | Out-Null
            $childText = $csb.ToString()
            Write-Output ("  Child " + $i + ": '" + $childText + "' (handle: " + $child + ")")
        }
        $i++
    }
} while ($child -ne [IntPtr]::Zero -and $i -lt 100)

Write-Output ("=== TOTAL CHILD WINDOWS ENUMERATED: " + $i + " ===")

# Also try to get text via SendMessage (WM_GETTEXT)
Write-Output "=== WM_GETTEXT FOR CHILD WINDOWS ==="
$WM_GETTEXT = 0x000D
$child2 = [IntPtr]::Zero
$j = 0
do {
    $child2 = [WinGet]::FindWindowEx($tvHandle, $child2, [NullString]::Value, [NullString]::Value)
    if ($child2 -ne [IntPtr]::Zero) {
        $buf = New-Object System.Text.StringBuilder 1024
        $result = [WinGet]::SendMessage($child2, $WM_GETTEXT, [IntPtr]::Zero, $buf)
        if ($result -gt 0) {
            Write-Output ("  WM_GETTEXT child " + $j + ": '" + $buf.ToString() + "'")
        }
        $j++
    }
} while ($child2 -ne [IntPtr]::Zero -and $j -lt 100)

Write-Output ("=== TOTAL WM_GETTEXT CHILDREN: " + $j + " ===")

# Take a screenshot
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$path = [System.IO.Path]::GetTempPath() + "tv_final.png"
$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $screen.Width, $screen.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen(0, 0, 0, 0, $screen.Size)
$bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()

Write-Output ("=== SCREENSHOT SAVED TO: " + $path + " ===")
$bytes = [System.IO.File]::ReadAllBytes($path)
$b64 = [System.Convert]::ToBase64String($bytes)
Write-Output "SCREENSHOT_BASE64_START"
Write-Output $b64
Write-Output "SCREENSHOT_BASE64_END"