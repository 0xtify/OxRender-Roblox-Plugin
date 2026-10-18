# OxRender

Create Roblox renders with custom camera controls, lighting, particles, and PNG export.

**Install the plugin:** [OxRender on Roblox](https://create.roblox.com/store/asset/78347126888023/OxRender)

## Requirements

- Roblox Studio
- Python 3.15.0 for the Windows export receiver
- `oxrender_receiver.py` and `oxrender_config.json`
- The **CreateAssetAsync Lua API** beta feature for workflows that depend on Roblox's programmatic asset creation API

## Roblox Studio Setup

### 1. Enable the CreateAssetAsync Lua API

If your OxRender workflow uses Roblox's asset creation or upload API:

1. Open Roblox Studio.
2. Go to **File → Beta Features**.
3. Find and enable **CreateAssetAsync Lua API**.
4. Restart Roblox Studio.

**Important:** Roblox currently restricts `AssetService:CreateAssetAsync()` to locally loaded plugins. It does not currently work from plugins installed directly through the Creator Store. Enabling the beta feature does not remove this restriction.

This beta is for Roblox asset creation and uploading; it is not needed just to save a PNG image to your computer.

See the [official Roblox AssetService documentation](https://create.roblox.com/docs/reference/engine/classes/AssetService) for details.

### 2. HTTP Requests

OxRender uses a local Python receiver to handle PNG exports.

When Roblox Studio asks for permission to communicate with a web address, allow the connection to the local receiver only if you trust the plugin and the receiver you're running. You can review or revoke plugin network permissions through Studio's plugin management controls.

**Do I need to enable Allow HTTP Requests?**

Generally, you do not need to enable your game's **Allow HTTP Requests** setting just for a Studio plugin to communicate with its local receiver. Roblox handles HTTP access from Studio plugins separately from HTTP requests made by scripts inside your experience.

Only enable the experience setting when your game's own scripts need to make HTTP requests.

See [Roblox's HTTP Service documentation](https://create.roblox.com/docs/cloud-services/http-service).

### 3. Mesh / Image APIs

You generally do not need to enable **Allow Mesh / Image APIs** just to export PNG renders through the local Python receiver.

This setting is intended for Roblox's EditableMesh and EditableImage APIs in experiences. Enable it only when a feature you are using specifically requires those APIs, and review Roblox's requirements and terms before doing so.

See the [EditableImage documentation](https://create.roblox.com/docs/reference/engine/classes/EditableImage) for more information.

## Python Downloads

Choose the appropriate download for your operating system.

- **Windows:** [Download Python Install Manager](https://www.python.org/downloads/latest/pymanager/)
- **macOS:** [Download the Python 3.15.0 installer](https://www.python.org/ftp/python/3.15.0/python-3.15.0-macos11.pkg)
- **Source release:** [Download the XZ compressed source tarball](https://www.python.org/ftp/python/3.15.0/Python-3.15.0.tar.xz)

For most Windows users, the Python Install Manager is the recommended option. The setup instructions below cover Windows.

## Setup (Windows)

1. Install Python 3.15.0 (64-bit) using the Python Install Manager. Make sure the `py` command is available.
2. Download [`oxrender_receiver.py`](./oxrender_receiver.py) and [`oxrender_config.json`](./oxrender_config.json) from this repository.
3. Place both files in the same folder. If you use background removal, add your own API key to the configuration file.
4. Open PowerShell and run the following command to locate and start the receiver:

   ```powershell
   $helper = Get-ChildItem "$env:USERPROFILE\Downloads" -Filter "oxrender_receiver.py" -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1

   if ($helper) {
       Set-Location $helper.DirectoryName
       py .\oxrender_receiver.py
   } else {
       Write-Host "Download oxrender_receiver.py into Downloads first."
   }
   ```

5. Keep the PowerShell window open while using OxRender in Roblox Studio.
6. If Roblox Studio prompts you to authorize the local connection, review the request before allowing it.

**Tip:** If the receiver does not start, verify that Python is installed correctly by running `py --version` in PowerShell.

## Finding Your Renders

Your exported PNG images are saved in:

`Pictures\OxRenderExports`

To find the exact location of a render:

1. Open Roblox Studio.
2. Enable **View → Output**.
3. Capture or export a render using OxRender.
4. Find the `[OxRender]` message containing the saved image's full path.
5. Copy that path into File Explorer to open the exported image.

## Troubleshooting

**The receiver does not start:** Check your Python installation and make sure you downloaded both receiver files.

**The local connection fails:** Ensure the Python receiver is running and review OxRender's network permissions in Studio.

**An asset upload fails:** Verify that the `CreateAssetAsync Lua API` beta is enabled and Studio has been restarted. Remember that Roblox currently restricts this API to locally loaded plugins, so the Creator Store version cannot use it directly.

**The PNG cannot be found:** Enable the Output window and look for the `[OxRender]` message containing the full file path.

---

That's it! Enjoy OxRender.
