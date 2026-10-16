# OxRender

Create clean Roblox renders with custom camera controls, lighting, particles, and PNG export.

**Get the plugin:** [Install OxRender from Roblox](https://create.roblox.com/store/asset/78347126888023/OxRender)

## Setup

1. Install [Python](https://www.python.org/downloads/windows/) for Windows.
2. Download [`oxrender_receiver.py`](oxrender_receiver.py) and [`oxrender_config.example.json`](oxrender_config.example.json) from this repository.
3. Put both files in the same folder. Rename `oxrender_config.example.json` to `oxrender_config.json` and add your own API key if using background removal.
4. Open PowerShell in that folder and run:

   ```powershell
   py .\oxrender_receiver.py
   ```

5. Keep PowerShell open while using OxRender in Roblox Studio.

Your PNG renders are saved in `Pictures\OxRenderExports`.

That's it! Enjoy creating renders with OxRender.
