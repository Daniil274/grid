"""
PowerShell-based Windows UI Automation helper.
Returns interactive elements from the foreground window as structured dicts.
File starts with _ so it is NOT loaded as a tool by the project tools loader.
"""

import json
import subprocess
from typing import List, Dict, Optional

_PS_ENCODING_PREFIX = r"""
$utf8 = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
"""

_PS_SCRIPT = r"""
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public class _WinHelper {
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
}
"@

$targetHwnd = "__HWND__"
if ($targetHwnd -and $targetHwnd -ne "0") {
    $hwnd = [IntPtr][int64]$targetHwnd
} else {
    $hwnd = [_WinHelper]::GetForegroundWindow()
}
if ($hwnd -eq [IntPtr]::Zero) { Write-Output "[]"; exit }

try {
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
} catch {
    Write-Output "[]"; exit
}
if (-not $root) { Write-Output "[]"; exit }

$interactiveTypes = @(
    "ControlType.Button", "ControlType.CheckBox", "ControlType.ComboBox",
    "ControlType.Edit", "ControlType.Hyperlink", "ControlType.ListItem",
    "ControlType.MenuItem", "ControlType.RadioButton", "ControlType.TabItem",
    "ControlType.TreeItem", "ControlType.DataItem", "ControlType.SplitButton"
)

$all = $root.FindAll(
    [System.Windows.Automation.TreeScope]::Subtree,
    [System.Windows.Automation.Condition]::TrueCondition
)

$out = [System.Collections.Generic.List[object]]::new()
$idx = 1
foreach ($el in $all) {
    $ct = $el.Current.ControlType.ProgrammaticName
    if ($interactiveTypes -notcontains $ct) { continue }
    $r = $el.Current.BoundingRectangle
    if ($r.IsEmpty -or $r.Width -le 2 -or $r.Height -le 2) { continue }
    $n = $el.Current.Name
    if (-not $n) { $n = $el.Current.AutomationId }
    if (-not $n) { $n = "" }
    $out.Add([pscustomobject]@{
        i   = $idx
        n   = [string]$n
        t   = ($ct -replace "ControlType\.", "")
        aid = [string]$el.Current.AutomationId
        x   = [int]($r.X + $r.Width / 2)
        y   = [int]($r.Y + $r.Height / 2)
        l   = [int]$r.X
        tp  = [int]$r.Y
        w   = [int]$r.Width
        h   = [int]$r.Height
    })
    $idx++
    if ($idx -gt 80) { break }
}
if ($out.Count -eq 0) { Write-Output "[]"; exit }
if ($out.Count -eq 1) { Write-Output ("[" + ($out[0] | ConvertTo-Json -Compress) + "]") }
else { $out | ConvertTo-Json -Compress }
"""


def get_uia_elements(timeout: int = 10, hwnd: Optional[int] = None) -> List[Dict]:
    """Run PowerShell UIAutomation and return element list."""
    try:
        script = _PS_ENCODING_PREFIX + _PS_SCRIPT.replace("__HWND__", str(int(hwnd or 0)))
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        raw = (result.stdout or "").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        return data if isinstance(data, list) else []
    except Exception:
        return []
