# Repackage the selected transparent artwork; no background removal or redraw.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$repoRoot = Split-Path -Parent $PSScriptRoot
$assetRoot = Join-Path $repoRoot 'assets'
$publicRoot = Join-Path $repoRoot 'apps/desktop/renderer/public/assets/branding'
$source = [System.Drawing.Bitmap]::new((Join-Path $assetRoot 'branding/kaze-master.png'))
try {
    if ($source.GetPixel(0, 0).A -ne 0) { throw 'The icon master must have a transparent background.' }
    [System.IO.Directory]::CreateDirectory($publicRoot) | Out-Null
    $sizes = @(16, 20, 24, 32, 40, 48, 64, 128, 256)
    $frames = @()
    foreach ($size in @($sizes) + @(512)) {
        $bitmap = [System.Drawing.Bitmap]::new($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $stream = [System.IO.MemoryStream]::new()
        try {
            $graphics.Clear([System.Drawing.Color]::Transparent)
            # Area-aware sampling keeps facial features readable at small sizes.
            $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
            $graphics.DrawImage($source, [System.Drawing.Rectangle]::new(0, 0, $size, $size))
            $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
            $bytes = $stream.ToArray()
            if ($size -le 256) { $frames += ,@{ Size = $size; Bytes = $bytes } }
            if ($size -eq 512) {
                [System.IO.File]::WriteAllBytes((Join-Path $assetRoot 'kaze-app-icon.png'), $bytes)
                [System.IO.File]::WriteAllBytes((Join-Path $publicRoot 'kaze-avatar.png'), $bytes)
            }
        } finally { $graphics.Dispose(); $bitmap.Dispose(); $stream.Dispose() }
    }
    $ico = [System.IO.File]::Create((Join-Path $assetRoot 'kaze-app-icon.ico'))
    $writer = [System.IO.BinaryWriter]::new($ico)
    try {
        $writer.Write([uint16]0); $writer.Write([uint16]1); $writer.Write([uint16]$frames.Count)
        $offset = 6 + 16 * $frames.Count
        foreach ($frame in $frames) {
            $dimension = if ($frame.Size -eq 256) { 0 } else { $frame.Size }
            $writer.Write([byte]$dimension); $writer.Write([byte]$dimension)
            $writer.Write([byte]0); $writer.Write([byte]0)
            $writer.Write([uint16]1); $writer.Write([uint16]32)
            $writer.Write([uint32]$frame.Bytes.Length); $writer.Write([uint32]$offset)
            $offset += $frame.Bytes.Length
        }
        foreach ($frame in $frames) { $writer.Write([byte[]]$frame.Bytes) }
    } finally { $writer.Dispose(); $ico.Dispose() }
    Copy-Item -LiteralPath (Join-Path $assetRoot 'kaze-app-icon.ico') -Destination (Join-Path $publicRoot 'kaze-favicon.ico')
    Write-Output "Generated PNG avatar and $($frames.Count)-size ICO from transparent master."
} finally { $source.Dispose() }
