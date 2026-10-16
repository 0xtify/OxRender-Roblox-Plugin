# OxRender

Create Roblox renders with custom camera controls, lighting, particles, and PNG export.

**Install the plugin:** [OxRender on Roblox](https://create.roblox.com/store/asset/78347126888023/OxRender)

## Setup

1. Install [Python 3.15.0 (64-bit)](https://www.python.org/downloads/release/python-3150/) for Windows. Make sure the `py` launcher is installed. (Scroll all the way down until you find the Installer Package)
2. Download `oxrender_receiver.py` and `oxrender_config.json` from this repository.
3. Put both files in the same folder. Add your own API key if using background removal.
4. Open PowerShell and run:

   ```powershell
   $helper = Get-ChildItem "$env:USERPROFILE\Downloads" -Filter "oxrender_receiver.py" -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
   if ($helper) {
       Set-Location $helper.DirectoryName
       py .\oxrender_receiver.py
   } else {
       Write-Host "Download oxrender_receiver.py into Downloads first."
   }
   ```

5. Keep PowerShell open while using OxRender.

PNG renders are saved to `Pictures\OxRenderExports`.

That's it! Enjoy OxRender.
