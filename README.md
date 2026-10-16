# OxRender

Create Roblox renders with custom camera controls, lighting, particles, and PNG export.

Install the plugin: [OxRender on Roblox](https://create.roblox.com/store/asset/78347126888023/OxRender)

## Python Downloads

Choose the download for your operating system. Windows is recommended for the OxRender setup instructions below.

- Windows: [Download Python Install Manager](https://www.python.org/downloads/latest/pymanager/)
- macOS: [Download macOS installer](https://www.python.org/ftp/python/3.15.0/python-3.15.0-macos11.pkg)
- Source release: [Download XZ compressed source tarball](https://www.python.org/ftp/python/3.15.0/Python-3.15.0.tar.xz)

## Setup (Windows)

1. Install Python 3.15.0 (64-bit) for Windows. Make sure the `py` launcher is available.
2. Download `oxrender_receiver.py` and `oxrender_config.json` from this repository.
3. Put both files in the same folder. Add your own API key to the config only if you use background removal.
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

5. Keep PowerShell open while using OxRender in Roblox Studio.

## Finding Your Renders

PNG renders are saved in `Pictures\OxRenderExports`.

To find the exact path of a saved image, open Roblox Studio and enable View > Output. Capture or export a render, then look for the `[OxRender]` message containing the saved image's full path. You can copy that path to find the image in File Explorer.

That's it! Enjoy OxRender.
