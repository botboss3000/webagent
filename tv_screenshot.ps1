Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
$path = [System.IO.Path]::GetTempPath() + "tv_final.png"
$screen = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $screen.Width, $screen.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen(0, 0, 0, 0, $screen.Size)
$bmp.Save($path, [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
Write-Output ("SCREENSHOT: " + $path)
Write-Output ("SIZE: " + $screen.Width + "x" + $screen.Height)