# Approval popup for `lab watch`: one monitor blurred (no tint), one Apple-style card on top.
# Exit codes: 10 approved, 11 denied (Esc / closed), 12 timed out, 13 self-test could not focus, 20 error.
param([Parameter(Mandatory = $true)][string]$Payload)
$ErrorActionPreference = 'Stop'

try {
    Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, System.Windows.Forms, System.Drawing
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class LabWin {
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern void keybd_event(byte vk, byte scan, uint flags, UIntPtr extra);
    [DllImport("gdi32.dll")] public static extern bool DeleteObject(IntPtr obj);
}
"@
    # Must happen before any window exists: otherwise Windows bitmap-scales everything on high-DPI screens.
    [void][LabWin]::SetProcessDPIAware()

    $p = Get-Content -Raw -LiteralPath $Payload | ConvertFrom-Json
    $script:code = 11

    # ---- theme -------------------------------------------------------------------------------------
    $light = $true
    try {
        $light = ((Get-ItemPropertyValue 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize' 'AppsUseLightTheme') -eq 1)
    } catch { }
    if ($p.theme -eq 'light') { $light = $true } elseif ($p.theme -eq 'dark') { $light = $false }
    if ($light) {
        $c = @{ card = '#F7FFFFFF'; edge = '#1F000000'; fg = '#1D1D1F'; fg2 = '#515154'; fg3 = '#86868B'; panel = '#F2F2F7';
                btn2 = '#E8E8ED'; btn2h = '#DCDCE2'; blue = '#007AFF'; blueh = '#0A6BE0'; red = '#FF3B30'; redh = '#E02D23'; dim = '#40000000' }
    } else {
        $c = @{ card = '#F5232325'; edge = '#2EFFFFFF'; fg = '#F5F5F7'; fg2 = '#C7C7CC'; fg3 = '#8E8E93'; panel = '#2C2C2E';
                btn2 = '#3A3A3C'; btn2h = '#48484A'; blue = '#0A84FF'; blueh = '#409CFF'; red = '#FF453A'; redh = '#FF6961'; dim = '#73000000' }
    }
    $primary = if ($p.danger) { $c.red } else { $c.blue }
    $primaryHover = if ($p.danger) { $c.redh } else { $c.blueh }

    # ---- the blurred backdrop: snapshot all screens, shrink (cheap, smooth), let WPF blur it ---------------
    # one monitor only: the one the mouse is on (that is where you are working), or the primary one
    $screen = if ($p.monitor -eq 'primary') { [System.Windows.Forms.Screen]::PrimaryScreen }
              else { [System.Windows.Forms.Screen]::FromPoint([System.Windows.Forms.Cursor]::Position) }
    $vs = $screen.Bounds   # physical pixels (the process is DPI aware)
    $shot = New-Object System.Drawing.Bitmap $vs.Width, $vs.Height
    $gfx = [System.Drawing.Graphics]::FromImage($shot)
    if ($p.backdrop -eq 'gradient') {
        # a neutral wallpaper instead of the real desktop (used for documentation screenshots)
        $gfx.SmoothingMode = 'AntiAlias'
        $rect = New-Object System.Drawing.Rectangle 0, 0, $vs.Width, $vs.Height
        $lg = New-Object System.Drawing.Drawing2D.LinearGradientBrush($rect, [System.Drawing.Color]::FromArgb(255, 24, 36, 92),
            [System.Drawing.Color]::FromArgb(255, 138, 52, 140), 45.0)
        $gfx.FillRectangle($lg, $rect)
        $blobs = @(@(0.18, 0.25, 0.55, 255, 90, 60, 170), @(0.78, 0.2, 0.45, 255, 255, 120, 90), @(0.55, 0.8, 0.6, 255, 40, 170, 230),
                   @(0.1, 0.85, 0.4, 255, 200, 80, 190))
        foreach ($b in $blobs) {
            $d = [int]($vs.Width * $b[2]); $x = [int]($vs.Width * $b[0] - $d / 2); $y = [int]($vs.Height * $b[1] - $d / 2)
            $br = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(150, $b[4], $b[5], $b[6]))
            $gfx.FillEllipse($br, $x, $y, $d, $d)
        }
    } else {
        $gfx.CopyFromScreen($vs.Left, $vs.Top, 0, 0, $shot.Size)
    }
    $gfx.Dispose()
    $sw = [int][Math]::Max(16, $vs.Width / 5)
    $sh = [int][Math]::Max(16, $vs.Height / 5)
    $small = New-Object System.Drawing.Bitmap $sw, $sh
    $gfx = [System.Drawing.Graphics]::FromImage($small)
    $gfx.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $gfx.DrawImage($shot, 0, 0, $sw, $sh)
    $gfx.Dispose()
    $shot.Dispose()
    $hbm = $small.GetHbitmap()
    $bgSource = [System.Windows.Interop.Imaging]::CreateBitmapSourceFromHBitmap($hbm, [IntPtr]::Zero,
        [System.Windows.Int32Rect]::Empty, [System.Windows.Media.Imaging.BitmapSizeOptions]::FromEmptyOptions())
    $bgSource.Freeze()
    [void][LabWin]::DeleteObject($hbm)
    $small.Dispose()

    # ---- window ----------------------------------------------------------------------------------------------
    $xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        WindowStyle="None" ResizeMode="NoResize" ShowInTaskbar="False" Topmost="True" Title="Lab approval"
        Background="#000000" WindowStartupLocation="Manual" UseLayoutRounding="True" SnapsToDevicePixels="True"
        TextOptions.TextFormattingMode="Ideal" FontFamily="Segoe UI Variable Display, Segoe UI">
  <Grid x:Name="Root" Opacity="0">
    <Grid ClipToBounds="True">
      <Image x:Name="Bg" Stretch="Fill" Margin="-60" RenderOptions.BitmapScalingMode="HighQuality">
        <Image.Effect><BlurEffect Radius="22" KernelType="Gaussian" RenderingBias="Performance"/></Image.Effect>
      </Image>
    </Grid>
    <Border x:Name="Card" Width="440" HorizontalAlignment="Left" VerticalAlignment="Top" CornerRadius="22"
            Background="[[CARD]]" BorderBrush="[[EDGE]]" BorderThickness="1" RenderTransformOrigin="0.5,0.5">
      <Border.RenderTransform><ScaleTransform x:Name="Scale" ScaleX="0.94" ScaleY="0.94"/></Border.RenderTransform>
      <Border.Effect><DropShadowEffect BlurRadius="64" ShadowDepth="16" Direction="270" Opacity="0.38" Color="Black"/></Border.Effect>
      <StackPanel Margin="30,30,30,22">
        <TextBlock x:Name="Glyph" FontFamily="Segoe Fluent Icons, Segoe MDL2 Assets" FontSize="46" Foreground="[[PRIMARY]]"
                   HorizontalAlignment="Center" Margin="0,2,0,0"/>
        <TextBlock x:Name="Title" Margin="0,18,0,0" FontSize="21" FontWeight="SemiBold" TextAlignment="Center"
                   TextWrapping="Wrap" Foreground="[[FG]]"/>
        <TextBlock x:Name="Subtitle" Margin="0,8,0,0" FontSize="14" LineHeight="20" TextAlignment="Center"
                   TextWrapping="Wrap" Foreground="[[FG2]]"/>
        <StackPanel x:Name="Toggle" Orientation="Horizontal" HorizontalAlignment="Center" Margin="0,16,0,0"
                    Cursor="Hand" Background="Transparent">
          <TextBlock x:Name="ToggleText" Text="More info" FontSize="13" Foreground="[[BLUE]]"/>
          <TextBlock Text="&#xE70D;" FontFamily="Segoe Fluent Icons, Segoe MDL2 Assets" FontSize="10" Margin="6,2,0,0"
                     Foreground="[[BLUE]]" VerticalAlignment="Center" RenderTransformOrigin="0.5,0.5">
            <TextBlock.RenderTransform><RotateTransform x:Name="Chevron" Angle="0"/></TextBlock.RenderTransform>
          </TextBlock>
        </StackPanel>
        <Border x:Name="Info" Height="0" ClipToBounds="True">
          <Border x:Name="InfoInner" Margin="0,12,0,0" CornerRadius="12" Background="[[PANEL]]" Padding="14,12">
            <StackPanel>
              <Grid x:Name="Rows"><Grid.ColumnDefinitions>
                <ColumnDefinition Width="112"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions></Grid>
              <TextBlock x:Name="Full" Margin="0,10,0,0" FontFamily="Cascadia Mono, Consolas" FontSize="12"
                         TextWrapping="Wrap" Foreground="[[FG2]]"/>
            </StackPanel>
          </Border>
        </Border>
        <Grid Margin="0,22,0,0">
          <Grid.ColumnDefinitions><ColumnDefinition Width="*"/><ColumnDefinition Width="12"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
          <Border x:Name="Deny" Grid.Column="0" Height="46" CornerRadius="13" Background="[[BTN2]]" Cursor="Hand">
            <TextBlock Text="Deny" FontSize="15" FontWeight="Medium" Foreground="[[FG]]" HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </Border>
          <Border x:Name="Approve" Grid.Column="2" Height="46" CornerRadius="13" Background="[[PRIMARY]]" Cursor="Hand">
            <TextBlock Text="Approve" FontSize="15" FontWeight="SemiBold" Foreground="White" HorizontalAlignment="Center" VerticalAlignment="Center"/>
          </Border>
        </Grid>
        <TextBlock Margin="0,14,0,0" FontSize="12" TextAlignment="Center" Foreground="[[FG3]]"
                   Text="Return to approve   &#x00B7;   Esc to deny"/>
      </StackPanel>
    </Border>
  </Grid>
</Window>
'@
    $subst = @{ 'DIM' = $c.dim; 'CARD' = $c.card; 'EDGE' = $c.edge; 'FG' = $c.fg; 'FG2' = $c.fg2; 'FG3' = $c.fg3;
                'PANEL' = $c.panel; 'BTN2' = $c.btn2; 'BLUE' = $c.blue; 'PRIMARY' = $primary }
    foreach ($k in $subst.Keys) { $xaml = $xaml.Replace("[[$k]]", $subst[$k]) }
    $win = [System.Windows.Markup.XamlReader]::Parse($xaml)

    # window = exactly that monitor, in device-independent units (system DPI is what WPF scales by)
    $dpi = [System.Drawing.Graphics]::FromHwnd([IntPtr]::Zero).DpiX
    $f = 96.0 / $dpi
    $win.Left = $vs.Left * $f
    $win.Top = $vs.Top * $f
    $win.Width = $vs.Width * $f
    $win.Height = $vs.Height * $f

    $brush = New-Object System.Windows.Media.BrushConverter
    $find = { param($n) $win.FindName($n) }
    $root = & $find 'Root'; $card = & $find 'Card'; $scale = & $find 'Scale'; $bg = & $find 'Bg'
    $info = & $find 'Info'; $infoInner = & $find 'InfoInner'; $chevron = & $find 'Chevron'; $toggleText = & $find 'ToggleText'
    $approveBtn = & $find 'Approve'; $denyBtn = & $find 'Deny'; $toggle = & $find 'Toggle'

    $bg.Source = $bgSource
    (& $find 'Glyph').Text = [string][char]$(if ($p.danger) { 0xE7BA } else { 0xE72E })  # warning / lock
    (& $find 'Title').Text = [string]$p.title
    (& $find 'Subtitle').Text = [string]$p.subtitle
    (& $find 'Full').Text = [string]$p.plan

    # detail rows
    $rows = & $find 'Rows'
    $i = 0
    foreach ($d in $p.details) {
        $rows.RowDefinitions.Add((New-Object System.Windows.Controls.RowDefinition)) | Out-Null
        foreach ($pair in @(@(0, [string]$d.label, $c.fg3), @(1, [string]$d.value, $c.fg))) {
            $t = New-Object System.Windows.Controls.TextBlock
            $t.Text = $pair[1]; $t.FontSize = 13; $t.TextWrapping = 'Wrap'; $t.Margin = New-Object System.Windows.Thickness(0, 2, 0, 2)
            $t.Foreground = $brush.ConvertFromString($pair[2])
            [System.Windows.Controls.Grid]::SetRow($t, $i); [System.Windows.Controls.Grid]::SetColumn($t, $pair[0])
            $rows.Children.Add($t) | Out-Null
        }
        $i++
    }

    # ---- behaviour -----------------------------------------------------------------------------------------------
    $ease = New-Object System.Windows.Media.Animation.CubicEase
    $ease.EasingMode = 'EaseOut'
    $animate = {
        param($target, $property, $from, $to, $ms)
        $a = New-Object System.Windows.Media.Animation.DoubleAnimation($from, $to, [TimeSpan]::FromMilliseconds($ms))
        $a.EasingFunction = $ease
        $target.BeginAnimation($property, $a)
    }
    $script:open = $false
    $toggleInfo = {
        $script:open = -not $script:open
        $infoInner.Measure((New-Object System.Windows.Size(380, [double]::PositiveInfinity)))
        $h = $infoInner.DesiredSize.Height + 12
        if ($script:open) {
            & $animate $info ([System.Windows.FrameworkElement]::HeightProperty) 0 $h 220
            & $animate $chevron ([System.Windows.Media.RotateTransform]::AngleProperty) 0 180 220
            $toggleText.Text = 'Less info'
        } else {
            & $animate $info ([System.Windows.FrameworkElement]::HeightProperty) $info.ActualHeight 0 200
            & $animate $chevron ([System.Windows.Media.RotateTransform]::AngleProperty) 180 0 200
            $toggleText.Text = 'More info'
        }
    }
    $finish = { param($n) $script:code = $n; $win.Close() }

    $toggle.Add_MouseLeftButtonUp($toggleInfo)
    $approveBtn.Add_MouseLeftButtonUp({ & $finish 10 })
    $denyBtn.Add_MouseLeftButtonUp({ & $finish 11 })
    $hover = { param($b, $on, $normal, $hot) $b.Background = $brush.ConvertFromString($(if ($on) { $hot } else { $normal })) }
    $approveBtn.Add_MouseEnter({ & $hover $approveBtn $true $primary $primaryHover })
    $approveBtn.Add_MouseLeave({ & $hover $approveBtn $false $primary $primaryHover })
    $denyBtn.Add_MouseEnter({ & $hover $denyBtn $true $c.btn2 $c.btn2h })
    $denyBtn.Add_MouseLeave({ & $hover $denyBtn $false $c.btn2 $c.btn2h })

    $win.Add_PreviewKeyDown({
        param($s, $e)
        switch ($e.Key) {
            'Return' { $e.Handled = $true; & $finish 10 }
            'Escape' { $e.Handled = $true; & $finish 11 }
            'I'      { $e.Handled = $true; & $toggleInfo }
        }
    })

    $win.Add_ContentRendered({
        # Windows refuses focus to background processes; a synthetic Alt press lifts that restriction.
        [LabWin]::keybd_event(0x12, 0, 0, [UIntPtr]::Zero); [LabWin]::keybd_event(0x12, 0, 2, [UIntPtr]::Zero)
        [void][LabWin]::SetForegroundWindow((New-Object System.Windows.Interop.WindowInteropHelper($win)).Handle)
        [void]$win.Activate()
        [System.Media.SystemSounds]::Asterisk.Play()
    })

    $win.Add_Loaded({
        # center the card on the primary monitor, a little above the middle (like a macOS alert)
        $win.UpdateLayout()
        $left = $win.ActualWidth / 2 - $card.ActualWidth / 2
        $top = $win.ActualHeight * 0.44 - $card.ActualHeight / 2
        $card.Margin = New-Object System.Windows.Thickness($left, [Math]::Max(20, $top), 0, 0)
        & $animate $root ([System.Windows.UIElement]::OpacityProperty) 0 1 180
        & $animate $scale ([System.Windows.Media.ScaleTransform]::ScaleXProperty) 0.94 1 240
        & $animate $scale ([System.Windows.Media.ScaleTransform]::ScaleYProperty) 0.94 1 240
        if ($p.expanded) { & $toggleInfo }
    })

    $timers = @()
    if ($p.autoclose) {
        $t = New-Object System.Windows.Threading.DispatcherTimer
        $t.Interval = [TimeSpan]::FromSeconds([double]$p.autoclose)
        $t.Add_Tick({ & $finish 12 })
        $t.Start(); $timers += $t
    }
    if ($p.expires_in) {
        $t = New-Object System.Windows.Threading.DispatcherTimer
        $t.Interval = [TimeSpan]::FromSeconds([double]$p.expires_in)
        $t.Add_Tick({ & $finish 12 })
        $t.Start(); $timers += $t
    }
    if ($p.selftest -eq 'enter') {
        # only press Return if this window really has the focus, so a test can never type into another app
        $t = New-Object System.Windows.Threading.DispatcherTimer
        $t.Interval = [TimeSpan]::FromMilliseconds(1800)
        $t.Add_Tick({ param($sender, $e) $sender.Stop(); if ($win.IsActive) { [System.Windows.Forms.SendKeys]::SendWait('{ENTER}') } else { & $finish 13 } })
        $t.Start(); $timers += $t
    }

    [void]$win.ShowDialog()
    exit $script:code
} catch {
    [Console]::Error.WriteLine($_.Exception.Message + ' @ ' + $_.InvocationInfo.PositionMessage)
    exit 20
}
