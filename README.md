# OxRender

Create Roblox renders with custom camera controls, lighting, particles, and PNG export.

**Install the plugin:** [OxRender on Roblox](https://create.roblox.com/store/asset/78347126888023/OxRender)

## Setup

1. Install Python. [Official Python 3.15.0 website](https://www.python.org/downloads/release/python-3150/)

   Downloads:
   - [Windows - Python install manager](https://www.python.org/downloads/latest/pymanager/)
   - [macOS - Installer](https://www.python.org/ftp/python/3.15.0/python-3.15.0-macos11.pkg)
   - [Source release - XZ tarball](https://www.python.org/ftp/python/3.15.0/Python-3.15.0.tar.xz)

   For OxRender's current capture helper, use Windows. After installing the Python install manager, run `py install 3.15` in PowerShell.

2. Download [oxrender_receiver.py](oxrender_receiver.py) and [oxrender_config.json](oxrender_config.json) from this repository.
3. Put both files in the same folder. Add your own API key to the config if using background removal.
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

5. Keep PowerShell open while using OxRender in Studio.

PNG renders are saved to `Pictures\OxRenderExports`.

Never upload API keys publicly. Enjoy OxRender!
