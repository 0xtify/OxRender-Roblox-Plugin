# OxRender

Create Roblox renders with custom camera controls, lighting, particles, and PNG export.

Install the plugin: [OxRender on Roblox](https://create.roblox.com/store/asset/78347126888023/OxRender)

## Setup

1. Install the [Python Install Manager for Windows](https://www.python.org/downloads/latest/pymanager/). Follow the installation instructions and install a Python runtime.
2. Download [oxrender_receiver.py](oxrender_receiver.py) and [oxrender_config.json](oxrender_config.json) from this repository.
3. Put both files in the same folder. Add your own API key to the configuration if you use background removal.
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
